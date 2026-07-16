from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Iterable, TypeAlias, overload

from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

# TODO: migrate this graph to the pydantic_graph GraphBuilder (Step/Decision/Edge)
# API once pydantic-graph v2 is officially released. The BaseNode `Graph` runner is
# deprecated for v2; pinned <2 in pyproject.toml until then.
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.capabilities.events import StreamEvents
from octomate.capabilities.gate import GateCapability
from octomate.config.agents import AgentRouteModelName
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.thread import Thread
from octomate.schemas.triage import (
    ResponseTargetMode,
    RunName,
    SummonDecision,
    SummonRoute,
)
from octomate.telemetry import reflex_logfire
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.output import split_reply
from octomate.reflex.suspender import HumanReviewSuspender, TeleportRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResponseTarget:
    channel_id: str
    address: ChannelAddress | None = None
    thread_strategy: ThreadStrategy = "main_only"
    mode: ResponseTargetMode = "main"

    def __str__(self) -> str:
        chat_type = self.address.chat_type if self.address else "unresolved"
        return (
            f"- {self.channel_id}: chat_type={chat_type}, mode={self.mode}, "
            f"thread_strategy={self.thread_strategy}"
        )


@dataclass
class ReflexResult:
    decision: SummonDecision | None
    target: ResponseTarget
    result: AgentRunResult[ChannelOutput] | None = None


@dataclass
class DeferredResult:
    requests: DeferredToolRequests
    target: ResponseTarget
    # The name of the run that deferred — carried for observability. `str`, not
    # `RunName`, because on a re-present it is read back from the persisted batch.
    run_name: str
    result: AgentRunResult[Any]
    batch_id: uuid.UUID | None = None


ReflexGraphResult: TypeAlias = ReflexResult | DeferredResult


@dataclass
class ReflexState:
    """All run-wide context for one reflex graph run.

    Awake resolves the source context once and writes it here; downstream nodes
    read from state and carry only transition discriminators.
    """

    source_target: ResponseTarget | None = None
    target: ResponseTarget | None = None
    run_name: RunName = "react"
    decision: SummonDecision | None = None
    targets: dict[str, ResponseTarget] = field(default_factory=dict)
    summon_routes: list[SummonRoute] = field(default_factory=list)
    thread: Thread | None = None
    trigger_thread_message_id: uuid.UUID | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[uuid.UUID] = field(default_factory=list)
    claim_handoff: bool = False
    handoff_from_agent_tentacle_id: str | None = None
    user_prompt: str | Sequence[UserContent] | None = None


@dataclass
class ReflexDeps:
    channels: dict[str, ChannelTentacle]
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    conversation_manager: ConversationManager = field(
        default_factory=ConversationManager
    )
    thread_manager: ThreadManager = field(default_factory=ThreadManager)
    action_manager: DeferredActionManager = field(default_factory=DeferredActionManager)

    @overload
    def channel(self, target: ResponseTarget) -> ChannelTentacle: ...

    @overload
    def channel(self, target: str) -> ChannelTentacle: ...

    def channel(self, target: ResponseTarget | str) -> ChannelTentacle:
        channel_id = target.channel_id if isinstance(target, ResponseTarget) else target
        channel = self.channels.get(channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {channel_id!r}")
        return channel

    def agent(self, agent_id: str) -> AgentTentacle:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"unknown agent {agent_id!r}")
        return agent

    def agent_configs(self, channel_id: str) -> list[AgentModelConfig]:
        return [
            agent_config
            for agent_config in self.channel(channel_id).config.agents
            if agent_config.agent in self.agents
        ]

    @cached_property
    def available_routes(self) -> dict[str, list[SummonRoute]]:
        available: dict[str, list[SummonRoute]] = {}
        for channel_id in self.channels:
            routes: list[SummonRoute] = []
            for agent_config in self.agent_configs(channel_id):
                agent = self.agent(agent_config.agent)
                routes.append(
                    SummonRoute(
                        agent_id=agent_config.agent,
                        model=agent_config.model,
                        description=agent.description,
                    )
                )
            available[channel_id] = routes
        return available

    def resolve_agent(
        self,
        channel_id: str,
        agent_id: str | None,
        model: AgentRouteModelName | None,
    ) -> AgentModelConfig:
        configs = self.agent_configs(channel_id)
        matched: AgentModelConfig | None = None
        for agent_config in configs:
            if agent_config.agent == agent_id and agent_config.model == model:
                return agent_config
            if agent_id is not None and agent_config.agent == agent_id:
                matched = agent_config
        return matched or configs[0]

    async def load_pending_prompt(
        self,
        state: ReflexState,
        active_agent_id: str,
    ) -> None:
        source_target = state.source_target
        if (
            state.thread is None
            or state.trigger_thread_message_id is None
            or source_target is None
            or source_target.address is None
        ):
            return
        # Pull every recorded chat-ledger row that has not been bound into a
        # model request yet: rule-gated group messages, sleeping/not-kicked
        # messages, and messages that stacked up behind an already-running turn.
        messages = await self.thread_manager.pending_prompt_messages(
            state.thread,
            state.trigger_thread_message_id,
            active_agent_id,
        )
        if not messages:
            return
        state.source_thread_address = source_target.address
        state.source_thread_message_ids = [message.id for message in messages]

        parts: list[str] = []
        for message in messages:
            text = message.message_text or message.raw
            if not text:
                continue
            display_name = message.sender.name or message.sender.nickname or "anonymous"
            platform_id = (
                f" #msg:{message.platform_message_id}"
                if message.platform_message_id
                else ""
            )
            parts.append(f"{display_name} ({message.user_id}){platform_id}:\n{text}")
        prompt = "\n\n".join(parts)
        if prompt:
            state.user_prompt = prompt


@dataclass
class Awake(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    signal: AwakeSignal

    @reflex_logfire.instrument("reflex.awake", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> Route | ResumeDeferred | End[ReflexGraphResult]:
        if isinstance(self.signal, DeferredActionBatchResponse):
            return ResumeDeferred(awake=self.signal)

        if not self.signal:
            reflex_logfire.info("awake short-circuit: empty signal")
            return End(
                ReflexResult(
                    decision=None,
                    target=ResponseTarget(channel_id=""),
                )
            )

        address = self.signal.address
        channel = ctx.deps.channels.get(address.channel_tentacle_id)
        if channel is None:
            raise ValueError(f"unknown channel {address.channel_tentacle_id!r}")

        source_target = ResponseTarget(
            channel_id=address.channel_tentacle_id,
            address=address,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
        ctx.state.source_target = source_target
        ctx.state.thread = await ctx.deps.thread_manager.ensure(address)
        ctx.state.trigger_thread_message_id = self.signal.trigger_thread_message_id

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        ctx.state.user_prompt = user_prompt
        if not user_prompt and self.signal.trigger_thread_message_id is None:
            reflex_logfire.info(
                "awake short-circuit: empty prompt",
                channel_id=address.channel_tentacle_id,
                conversation_address=str(address),
            )
            return End(
                ReflexResult(
                    decision=None,
                    target=source_target,
                )
            )
        return Route()


@dataclass
class Route(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    @reflex_logfire.instrument("reflex.route", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        source_target = state.source_target
        if source_target is None or source_target.address is None:
            raise ValueError("Route requires a resolved source target")

        source_address = source_target.address

        thread = state.thread
        active_agent_id = (
            thread.active_agent_tentacle_id if thread is not None else None
        )
        if thread is not None and active_agent_id is not None:
            active_model = thread.active_model
            ctx.deps.agent(active_agent_id)
            resolved = ctx.deps.resolve_agent(
                source_address.channel_tentacle_id,
                active_agent_id,
                active_model,
            )
            await ctx.deps.load_pending_prompt(state, resolved.agent)
            model = resolved.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Continuing with the active thread owner.",
                hint="Continuing with the active thread owner.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(
                source_target,
                mode="sub" if source_address.thread_id else "main",
            )
            state.claim_handoff = False
            state.handoff_from_agent_tentacle_id = None
            return React()

        if source_address.thread_id and source_target.thread_strategy == "flat_thread":
            reflex_logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_address.channel_tentacle_id,
                conversation_address=str(source_address),
            )
            resolved = ctx.deps.resolve_agent(
                source_address.channel_tentacle_id,
                None,
                None,
            )
            await ctx.deps.load_pending_prompt(state, resolved.agent)
            model = resolved.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Continuing in the current thread.",
                hint="Continuing in the current thread.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(source_target, mode="sub")
            state.claim_handoff = True
            state.handoff_from_agent_tentacle_id = None
            return React()

        # No active owner and not already in a flat thread: run the channel's default
        # agent directly in this conversation — no separate screening pass. It
        # self-routes via the gate toolset (summon / teleport) if it wants to hand off
        # or relocate. No handoff is recorded, so a group main is never pinned to an
        # owner (Case 1).
        resolved = ctx.deps.resolve_agent(source_address.channel_tentacle_id, None, None)
        await ctx.deps.load_pending_prompt(state, resolved.agent)
        state.decision = SummonDecision(
            action="summon",
            agent_id=resolved.agent,
            model=resolved.model,
            reason="Entry agent.",
            hint="",
            summon="",
        )
        state.target = replace(
            source_target, mode="sub" if source_address.thread_id else "main"
        )
        state.claim_handoff = False
        state.handoff_from_agent_tentacle_id = None
        return React()


@dataclass
class Handoff(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    @reflex_logfire.instrument("reflex.handoff", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        decision = state.decision
        target = state.target
        source_target = state.source_target
        if (
            decision is None
            or target is None
            or source_target is None
            or source_target.address is None
        ):
            raise ValueError("Handoff requires a decision and source target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("Handoff requires a summon decision")
        source_address = source_target.address
        channel = ctx.deps.channel(target)

        target_address = target.address
        if target_address is None:
            target_address = ChannelAddress(
                channel_tentacle_id=target.channel_id,
                chat_type=source_address.chat_type,
                chat_id=source_address.chat_id,
                user_id=source_address.user_id,
                thread_id="",
            )
            target = replace(target, address=target_address)

        if decision.destination == "here":
            # Take over the current conversation in place — no new surface. The
            # allow_here gate already refused this on a group main (Case 1).
            target = replace(target, address=target_address)
        elif target.thread_strategy == "main_only":
            target = replace(target, mode="main")
        elif not target_address.thread_id:
            try:
                target_address = await channel.start_sub_thread(
                    target_address,
                    decision.hint
                    or decision.reason
                    or "Octomate is continuing this request here.",
                )
                target = replace(target, address=target_address)
            except Exception:
                logger.warning(
                    "Channel %s failed to start a sub-thread; using main target",
                    target.channel_id,
                    exc_info=True,
                )
                target = replace(target, mode="main")
        else:
            target = replace(target, address=target_address)

        state.target = target
        if target.address is not None and state.thread is not None:
            state.thread = await ctx.deps.thread_manager.ensure(target.address)
        return React()


@dataclass
class React(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    resume_batch_id: uuid.UUID | None = None
    # Set by Teleport to resume the same agent against the forked history.
    teleport_results: DeferredToolResults | None = None

    @reflex_logfire.instrument("reflex.react", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> Handoff | Teleport | End[ReflexGraphResult]:
        state = ctx.state
        decision = state.decision
        target = state.target
        source_target = state.source_target
        if (
            decision is None
            or target is None
            or target.address is None
            or source_target is None
            or source_target.address is None
        ):
            raise ValueError("React requires a decision and resolved target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("React requires a summon decision")
        target_address = target.address
        source_address = source_target.address
        resolved = ctx.deps.resolve_agent(
            source_address.channel_tentacle_id, decision.agent_id, decision.model
        )
        model = resolved.model
        decision = decision.model_copy(
            update={"agent_id": resolved.agent, "model": model}
        )
        state.decision = decision
        agent = ctx.deps.agent(resolved.agent)
        run_model = agent.models.get(model)
        if run_model is None:
            raise ValueError(f"agent {agent.id!r} has no configured model {model!r}")
        thread_id = state.thread.id if state.thread else None
        if state.thread is not None and state.claim_handoff:
            target_conversation = await ctx.deps.conversation_manager.ensure(
                state.thread.id,
                agent_tentacle_id=agent.id,
            )
            latest_handoff = state.thread.latest_handoff
            target_model = model
            if (
                latest_handoff is None
                or latest_handoff.to_agent_tentacle_id != agent.id
                or latest_handoff.to_model != target_model
            ):
                await ctx.deps.thread_manager.record_handoff(
                    state.thread,
                    from_agent_tentacle_id=state.handoff_from_agent_tentacle_id,
                    to_agent_tentacle_id=agent.id,
                    to_model=target_model,
                    reason=decision.reason,
                    hint=decision.hint,
                    brief=decision.summon,
                    target_conversation_id=target_conversation.id,
                )
            state.claim_handoff = False
            state.handoff_from_agent_tentacle_id = None
        target_channel = ctx.deps.channel(target)
        routes = [
            route
            for route in ctx.deps.available_routes[source_address.channel_tentacle_id]
            if route.agent_id != agent.id
        ]
        state.summon_routes = routes
        # `summon here` is refused on a group main (Case 1): pinning an owner there
        # would route every gated-in message, from any user, to one agent.
        gate = GateCapability(
            routes=routes,
            current_agent_id=agent.id,
            allow_here=not (target_address.is_group and not target_address.thread_id),
        )

        deferred_results = self.teleport_results
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        if deferred_results is not None:
            user_prompt: str | Sequence[UserContent] | None = None
        else:
            user_prompt = decision.summon or str(state.user_prompt or "")

        suspender = HumanReviewSuspender(
            channel=target_channel,
            action_manager=ctx.deps.action_manager,
            conversation_manager=ctx.deps.conversation_manager,
            agent_tentacle_id=agent.id,
            run_name=state.run_name,
            source_address=source_address,
            target_address=target_address,
            target_mode=target.mode,
            decision=state.decision,
            thread_id=thread_id,
            emit_on_stream=target_channel.config.stream.enabled,
        )

        with reflex_logfire.span(
            "react",
            channel_id=target_address.channel_tentacle_id,
            agent_id=agent.id,
            conversation_address=str(target_address),
            streaming=target_channel.config.stream.enabled,
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            stream_results: list[AgentRunResult[ChannelOutput]] = []
            reply_thread_message_ids: list[uuid.UUID] = []
            assistant_replies_bound = False
            if target_channel.config.stream.enabled:

                async def stream_events() -> AsyncIterator[
                    StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
                ]:
                    async with agent.run_stream_events(
                        user_prompt,
                        conversation_address=target_address,
                        thread_id=thread_id,
                        source_thread_address=state.source_thread_address,
                        source_thread_message_ids=state.source_thread_message_ids,
                        run_name=state.run_name,
                        model=run_model,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                        capabilities=[gate],
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                stream_results.append(event.result)
                            yield event

                try:
                    async with target_channel.feelers.timeline.open(
                        target_address
                    ) as timeline_state:
                        await timeline_state.drive(stream_events())
                except AgentRunError:
                    # A model/provider failure (e.g. invalid Bedrock credentials)
                    # surfaces here from the run stream itself, not the render. It
                    # is the real cause — let it propagate instead of masking it as
                    # a render warning and a generic "no result" error below.
                    raise
                except Exception:
                    logger.warning(
                        "Channel %s: timeline render failed",
                        target_address.channel_tentacle_id,
                        exc_info=True,
                    )
                if not stream_results:
                    raise RuntimeError(
                        f"react stream for {target_address} completed without a result"
                    )
                run_result = stream_results[-1]
                run_output: ChannelOutput = run_result.output
                if isinstance(run_output, str):
                    if run_output:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=[MarkdownSegment(data={"text": run_output})],
                            message_text=run_output,
                            raw=run_output,
                        )
                        reply_thread_message_ids.append(thread_message.id)
                elif isinstance(run_output, Iterable):
                    segments = list(run_output)
                    _reply_to, body = split_reply(segments)
                    if body:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=body,
                            raw="\n\n".join(str(segment) for segment in body),
                        )
                        reply_thread_message_ids.append(thread_message.id)
            else:
                run_result = await agent.run(
                    user_prompt,
                    conversation_address=target_address,
                    thread_id=thread_id,
                    source_thread_address=state.source_thread_address,
                    source_thread_message_ids=state.source_thread_message_ids,
                    run_name=state.run_name,
                    model=run_model,
                    deferred_tool_results=deferred_results,
                    deferred_suspender=suspender,
                    capabilities=[gate],
                )
                run_output: ChannelOutput = run_result.output
                if isinstance(run_output, str):
                    if run_output:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=[MarkdownSegment(data={"text": run_output})],
                            message_text=run_output,
                            raw=run_output,
                        )
                        reply_thread_message_ids.append(thread_message.id)
                        await ctx.deps.thread_manager.bind_assistant_replies(
                            reply_thread_message_ids,
                            run_id=run_result.run_id,
                        )
                        assistant_replies_bound = True
                        message_id = await target_channel.feelers.markdown.present(
                            target_address,
                            run_output,
                        )
                        await ctx.deps.thread_manager.mark_presented(
                            thread_message,
                            message_id,
                        )
                elif isinstance(run_output, Iterable):
                    # A segment list is the only media-bearing reply: deliver it
                    # natively (channels without a media transport fall back to
                    # the joined text form in send_segments).
                    segments = list(run_output)
                    _reply_to, body = split_reply(segments)
                    thread_message = None
                    if body:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=body,
                            raw="\n\n".join(str(segment) for segment in body),
                        )
                        reply_thread_message_ids.append(thread_message.id)
                    await ctx.deps.thread_manager.bind_assistant_replies(
                        reply_thread_message_ids,
                        run_id=run_result.run_id,
                    )
                    assistant_replies_bound = True
                    message_id = await target_channel.feelers.segments.present(
                        target_address, segments
                    )
                    if thread_message is not None:
                        await ctx.deps.thread_manager.mark_presented(
                            thread_message,
                            message_id,
                        )
                # DeferredToolRequests / None: nothing to deliver here.

            output = run_result.output
            if self.resume_batch_id is not None:
                await ctx.deps.action_manager.mark_batch(
                    self.resume_batch_id,
                    "completed",
                    completed=True,
                )

            span.set_attribute("react.run_id", run_result.run_id)
            span.set_attribute(
                "react.deferred", isinstance(output, DeferredToolRequests)
            )
            if not assistant_replies_bound:
                await ctx.deps.thread_manager.bind_assistant_replies(
                    reply_thread_message_ids,
                    run_id=run_result.run_id,
                )
            if isinstance(output, DeferredToolRequests):
                # `teleport` is resolved by the graph (fork + resume), not a human. The
                # suspender classified it by its declared metadata kind and stashed it,
                # so route on the typed request instead of re-scanning tool names.
                if suspender.teleport is not None:
                    return Teleport(
                        request=suspender.teleport, origin=target, agent_id=agent.id
                    )
                return End(
                    DeferredResult(
                        requests=output,
                        target=target,
                        run_name=state.run_name,
                        result=run_result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            summon_decision = gate.decision
            if summon_decision is not None:
                state.decision = summon_decision
                state.target = target
                state.claim_handoff = True
                state.handoff_from_agent_tentacle_id = agent.id
                state.run_name = "summon"
                span.set_attribute("react.action", summon_decision.action)
                span.set_attribute("react.next_agent_id", summon_decision.agent_id)
                reflex_logfire.info(
                    "react -> {action} agent={agent_id}",
                    action=summon_decision.action,
                    agent_id=summon_decision.agent_id,
                    reason=summon_decision.reason,
                )
                return Handoff()

            return End(
                ReflexResult(
                    decision=decision,
                    target=target,
                    result=run_result,
                )
            )


@dataclass
class Teleport(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """A `teleport` deferred call: fork the running agent's history into a fresh
    sub-thread of the current chat and resume it there. When a new thread can't be
    opened (main_only, or already inside a thread), resolve in place and stay put."""

    request: TeleportRequest
    origin: ResponseTarget
    agent_id: str

    @reflex_logfire.instrument("reflex.teleport", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        state.run_name = "teleport"
        origin = self.origin
        if origin.address is None or state.thread is None:
            raise ValueError("Teleport requires a resolved origin and thread")
        origin_address = origin.address
        hint = self.request.hint or "Octomate is continuing this request here."

        new_target = origin
        if origin.thread_strategy != "main_only" and not origin_address.thread_id:
            try:
                new_address = await ctx.deps.channel(origin).start_sub_thread(
                    origin_address, hint
                )
                new_target = replace(origin, address=new_address, mode="sub")
            except Exception:
                logger.warning(
                    "Channel %s failed to open a teleport sub-thread; staying put",
                    origin.channel_id,
                    exc_info=True,
                )

        results = DeferredToolResults(
            calls={self.request.tool_call_id: "Continuing the conversation here."}
        )

        new_address = new_target.address
        if new_address is None or new_address == origin_address:
            # Stay put: the current conversation already holds the trailing teleport
            # deferral, so just resolve it and resume in place — nothing to fork.
            state.target = origin
            state.claim_handoff = False
            return React(teleport_results=results)

        # Move: fork the origin conversation into the new sub-thread, claim it for the
        # same agent so follow-ups continue there, and resume against the fork.
        new_thread = await ctx.deps.thread_manager.ensure(new_address)
        source_conversation = await ctx.deps.conversation_manager.ensure(
            state.thread.id, agent_tentacle_id=self.agent_id
        )
        target_conversation = await ctx.deps.conversation_manager.ensure(
            new_thread.id, agent_tentacle_id=self.agent_id
        )
        await ctx.deps.conversation_manager.fork(
            source_conversation, target_conversation
        )
        state.thread = new_thread
        state.target = new_target
        state.claim_handoff = True
        state.handoff_from_agent_tentacle_id = self.agent_id
        return React(teleport_results=results)


@dataclass
class ResumeDeferred(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    awake: DeferredActionBatchResponse

    @reflex_logfire.instrument("reflex.resume_deferred", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | End[ReflexGraphResult]:
        state = ctx.state
        with reflex_logfire.span("resume_deferred", batch_id=str(self.awake.batch_id)) as span:
            batch = await ctx.deps.action_manager.resolve_batch(self.awake)
            span.set_attribute("run_name", batch.run_name)
            span.set_attribute("batch_status", batch.status)
            span.set_attribute("completed", batch.completed)
        target_channel = ctx.deps.channels.get(batch.target_address.channel_tentacle_id)
        if target_channel is None:
            raise ValueError(
                f"unknown channel {batch.target_address.channel_tentacle_id!r}"
            )
        ctx.deps.agent(batch.agent_tentacle_id)
        target = ResponseTarget(
            channel_id=batch.target_address.channel_tentacle_id,
            address=batch.target_address,
            thread_strategy=target_channel.thread_strategy,
            mode=batch.target_mode,
        )
        source_channel = ctx.deps.channels.get(batch.source_address.channel_tentacle_id)
        state.target = target
        state.source_target = ResponseTarget(
            channel_id=batch.source_address.channel_tentacle_id,
            address=batch.source_address,
            thread_strategy=(
                source_channel.thread_strategy
                if source_channel is not None
                else target_channel.thread_strategy
            ),
            mode="main",
        )

        if isinstance(batch.decision, SummonDecision):
            decision = batch.decision
        elif batch.decision:
            decision = SummonDecision.model_validate(batch.decision)
        else:
            resolved = ctx.deps.resolve_agent(
                batch.source_address.channel_tentacle_id,
                batch.agent_tentacle_id,
                None,
            )
            model = resolved.model
            decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Resuming deferred human input.",
                hint="Resuming deferred human input.",
                summon="",
            )
        if not isinstance(decision, SummonDecision):
            raise ValueError("react deferred batch requires a summon decision")
        decision = decision.model_copy(update={"agent_id": batch.agent_tentacle_id})
        state.decision = decision
        if batch.status in {"completed", "resuming"}:
            reflex_logfire.info("resume already completed", batch_id=str(batch.id))
            return End(ReflexResult(decision=decision, target=target))
        if not batch.completed:
            return End(
                DeferredResult(
                    requests=batch.requests,
                    target=target,
                    run_name=batch.run_name or "react",
                    result=AgentRunResult(batch.requests),
                    batch_id=batch.id,
                )
            )

        await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
        # The resumed agent run continues the batch's conversation, so bind the
        # thread that owns it (React reads state.thread for the run's thread_id).
        state.thread = await ctx.deps.thread_manager.ensure(batch.target_address)
        state.user_prompt = None
        state.run_name = "resume"
        reflex_logfire.info("resume routes to React", batch_id=str(batch.id))
        return React(resume_batch_id=batch.id)


reflex_graph = Graph[ReflexState, ReflexDeps, ReflexGraphResult](
    nodes=[
        Awake,
        Route,
        Handoff,
        React,
        Teleport,
        ResumeDeferred,
    ],
    name="reflex",
)
