from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Iterable, Literal, TypeAlias, cast, overload

import logfire
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import UserContent
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests

# TODO: migrate this graph to the pydantic_graph GraphBuilder (Step/Decision/Edge)
# API once pydantic-graph v2 is officially released. The BaseNode `Graph` runner is
# deprecated for v2; pinned <2 in pyproject.toml until then.
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.capabilities.events import StreamEvents
from octomate.capabilities.summon import SummonCapability
from octomate.config.agents import AgentRouteModelName
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment, MessageSegment
from octomate.schemas.thread import Thread
from octomate.schemas.triage import (
    DirectAnswerDecision,
    ResponseTargetMode,
    SummonDecision,
    SummonRoute,
    TriageDecision,
    TriageDecisionAdapter,
)
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.output import split_reply
from octomate.triage.suspender import HumanReviewSuspender

logger = logging.getLogger(__name__)

# Triage adds a structured direct-answer decision on top of ordinary
# channel-renderable output.
TriageOutput: TypeAlias = ChannelOutput | DirectAnswerDecision


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
class TriageResult:
    decision: TriageDecision
    target: ResponseTarget
    result: AgentRunResult[TriageOutput] | AgentRunResult[ChannelOutput] | None = None


@dataclass
class DeferredResult:
    requests: DeferredToolRequests
    target: ResponseTarget
    run_name: Literal["triage", "reception"]
    result: AgentRunResult[Any]
    batch_id: uuid.UUID | None = None


TriageGraphResult: TypeAlias = TriageResult | DeferredResult


@dataclass
class TriageState:
    """All run-wide context for one triage graph run.

    Awake resolves the source context once and writes it here; downstream nodes
    read from state and carry only transition discriminators.
    """

    source_target: ResponseTarget | None = None
    target: ResponseTarget | None = None
    decision: TriageDecision | None = None
    targets: dict[str, ResponseTarget] = field(default_factory=dict)
    summon_routes: list[SummonRoute] = field(default_factory=list)
    thread: Thread | None = None
    trigger_thread_message_id: uuid.UUID | None = None
    source_thread_address: ChannelAddress | None = None
    source_thread_message_ids: list[uuid.UUID] = field(default_factory=list)
    claim_handoff: bool = False
    handoff_from_agent_tentacle_id: str | None = None
    user_prompt: str | Sequence[UserContent] | None = None
    run_name: Literal["triage", "reception"] = "triage"


@dataclass
class TriageDeps:
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

    def triage(self, channel_id: str) -> AgentModelConfig:
        return self.channel(channel_id).config.triage

    def receptions(self, channel_id: str) -> list[AgentModelConfig]:
        return [
            reception
            for reception in self.channel(channel_id).config.receptions
            if reception.agent in self.agents
        ]

    @cached_property
    def available_routes(self) -> dict[str, list[SummonRoute]]:
        available: dict[str, list[SummonRoute]] = {}
        for channel_id in self.channels:
            routes: list[SummonRoute] = []
            for reception in self.receptions(channel_id):
                agent = self.agent(reception.agent)
                routes.append(
                    SummonRoute(
                        agent_id=reception.agent,
                        model=reception.model,
                        description=agent.description,
                    )
                )
            available[channel_id] = routes
        return available

    def reception(
        self,
        channel_id: str,
        agent_id: str | None,
        model: AgentRouteModelName | None,
    ) -> AgentModelConfig:
        receptions = self.receptions(channel_id)
        matched: AgentModelConfig | None = None
        for reception in receptions:
            if reception.agent == agent_id and reception.model == model:
                return reception
            if agent_id is not None and reception.agent == agent_id:
                matched = reception
        return matched or receptions[0]

    async def load_pending_prompt(
        self,
        state: TriageState,
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
class Awake(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    signal: AwakeSignal

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> Route | ResumeDeferred | End[TriageGraphResult]:
        if isinstance(self.signal, DeferredActionBatchResponse):
            return ResumeDeferred(awake=self.signal)

        if not self.signal:
            logfire.info("awake short-circuit: empty signal")
            return End(
                TriageResult(
                    decision=DirectAnswerDecision(
                        action="direct_answer",
                        target_id="",
                        answer="",
                        reason="Empty awake signal.",
                    ),
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
            logfire.info(
                "awake short-circuit: empty prompt",
                channel_id=address.channel_tentacle_id,
                conversation_address=str(address),
            )
            return End(
                TriageResult(
                    decision=DirectAnswerDecision(
                        action="direct_answer",
                        target_id=source_target.channel_id,
                        answer="",
                        reason="Empty user prompt.",
                    ),
                    target=source_target,
                )
            )
        return Route()


@dataclass
class Route(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunTriage | RunReception:
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
            reception = ctx.deps.reception(
                source_address.channel_tentacle_id,
                active_agent_id,
                active_model,
            )
            await ctx.deps.load_pending_prompt(state, reception.agent)
            model = reception.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=reception.agent,
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
            state.run_name = "reception"
            return RunReception()

        if source_address.thread_id and source_target.thread_strategy == "flat_thread":
            logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_address.channel_tentacle_id,
                conversation_address=str(source_address),
            )
            reception = ctx.deps.reception(
                source_address.channel_tentacle_id,
                None,
                None,
            )
            await ctx.deps.load_pending_prompt(state, reception.agent)
            model = reception.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=reception.agent,
                model=model,
                reason="Continuing in the current thread.",
                hint="Continuing in the current thread.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(source_target, mode="sub")
            state.claim_handoff = True
            state.handoff_from_agent_tentacle_id = None
            state.run_name = "reception"
            return RunReception()
        return RunTriage()


@dataclass
class RunTriage(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    resume_batch_id: uuid.UUID | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> PrepareReception | End[TriageGraphResult]:
        state = ctx.state
        source_target = state.source_target
        if source_target is None or source_target.address is None:
            raise ValueError("RunTriage requires a resolved source target")
        source_address = source_target.address
        source_channel_id = source_address.channel_tentacle_id
        triage = ctx.deps.triage(source_channel_id)
        agent = ctx.deps.agent(triage.agent)
        if self.resume_batch_id is None:
            await ctx.deps.load_pending_prompt(state, agent.id)
        triage_model = agent.models.get(triage.model)
        if triage_model is None:
            raise ValueError(
                f"agent {agent.id!r} has no configured model {triage.model!r}"
            )

        targets: dict[str, ResponseTarget] = {}
        for channel_id, channel in ctx.deps.channels.items():
            targets[channel_id] = (
                source_target
                if channel_id == source_target.channel_id
                else ResponseTarget(
                    channel_id=channel_id,
                    address=None,
                    thread_strategy=channel.thread_strategy,
                    mode="main",
                )
            )
        state.targets = targets

        routes = [
            route
            for route in ctx.deps.available_routes[source_channel_id]
            if route.agent_id != agent.id
        ]
        state.summon_routes = routes
        summon = SummonCapability(
            routes=routes,
            current_agent_id=agent.id,
        )

        suspender = HumanReviewSuspender(
            channel=ctx.deps.channel(source_target),
            action_manager=ctx.deps.action_manager,
            conversation_manager=ctx.deps.conversation_manager,
            agent_tentacle_id=agent.id,
            run_name="triage",
            source_address=source_address,
            target_address=source_address,
            target_mode="main",
            decision=None,
            thread_id=state.thread.id if state.thread else None,
        )
        deferred_results = None
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        with logfire.span(
            "triage",
            channel_id=source_address.channel_tentacle_id,
            agent_id=agent.id,
            conversation_address=str(source_address),
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            result = await agent.run(
                state.user_prompt,
                conversation_address=source_address,
                thread_id=(state.thread.id if state.thread else None),
                source_thread_address=state.source_thread_address,
                source_thread_message_ids=state.source_thread_message_ids,
                run_name="triage",
                output_type=cast(
                    OutputSpec[TriageOutput],
                    [
                        str,
                        list[MessageSegment],
                        DirectAnswerDecision,
                        DeferredToolRequests,
                    ],
                ),
                model=triage_model,
                deferred_tool_results=deferred_results,
                deferred_suspender=suspender,
                capabilities=[summon],
            )
            if self.resume_batch_id is not None:
                await ctx.deps.action_manager.mark_batch(
                    self.resume_batch_id,
                    "completed",
                    completed=True,
                )

            span.set_attribute("triage.run_id", result.run_id)
            span.set_attribute(
                "triage.deferred",
                isinstance(result.output, DeferredToolRequests),
            )
            if isinstance(result.output, DeferredToolRequests):
                return End(
                    DeferredResult(
                        requests=result.output,
                        target=source_target,
                        run_name="triage",
                        result=result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            output = result.output
            decision = summon.decision
            if decision is None:
                if isinstance(output, DirectAnswerDecision):
                    decision = output
                    target = targets.get(decision.target_id)
                    if target is None:
                        raise ValueError(
                            f"Invalid response target {decision.target_id!r}. "
                            f"Choose one of: {', '.join(repr(key) for key in targets)}."
                        )
                    answer = decision.answer
                elif isinstance(output, str):
                    target = source_target
                    answer = output
                    decision = DirectAnswerDecision(
                        action="direct_answer",
                        target_id=target.channel_id,
                        answer=answer,
                        reason="Answered directly.",
                    )
                elif isinstance(output, list):
                    target = source_target
                    answer = ""
                    decision = DirectAnswerDecision(
                        action="direct_answer",
                        target_id=target.channel_id,
                        answer="",
                        reason="Answered directly with message segments.",
                    )
                else:
                    raise TypeError(
                        "triage agent must return a direct answer or use the "
                        f"summon tool, got {type(output).__name__}"
                    )
                if target.address is None:
                    raise ValueError(
                        f"target {target.channel_id!r} has no resolved address"
                    )
                state.decision = decision
                state.target = target
                span.set_attribute("triage.action", decision.action)
                span.set_attribute("triage.target_id", decision.target_id)
                logfire.info(
                    "triage -> {action} target={target_id}",
                    action=decision.action,
                    target_id=decision.target_id,
                    reason=decision.reason,
                )
                reply_thread_message_ids: list[uuid.UUID] = []
                if isinstance(output, list):
                    _reply_to, body = split_reply(output)
                    thread_message = None
                    if body:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target.address,
                            agent_tentacle_id=agent.id,
                            segments=body,
                            raw="\n\n".join(str(segment) for segment in body),
                        )
                        reply_thread_message_ids.append(thread_message.id)
                    await ctx.deps.thread_manager.bind_assistant_replies(
                        reply_thread_message_ids,
                        run_id=result.run_id,
                    )
                    message_id = await ctx.deps.channel(
                        target
                    ).feelers.segments.present(target.address, output)
                    if thread_message is not None:
                        await ctx.deps.thread_manager.mark_presented(
                            thread_message,
                            message_id,
                        )
                elif answer:
                    thread_message = await ctx.deps.thread_manager.record_outbound(
                        target.address,
                        agent_tentacle_id=agent.id,
                        segments=[MarkdownSegment(data={"text": answer})],
                        message_text=answer,
                        raw=answer,
                    )
                    reply_thread_message_ids.append(thread_message.id)
                    await ctx.deps.thread_manager.bind_assistant_replies(
                        reply_thread_message_ids,
                        run_id=result.run_id,
                    )
                    message_id = await ctx.deps.channel(
                        target
                    ).feelers.markdown.present(
                        target.address,
                        answer,
                    )
                    await ctx.deps.thread_manager.mark_presented(
                        thread_message,
                        message_id,
                    )
                else:
                    await ctx.deps.thread_manager.bind_assistant_replies(
                        reply_thread_message_ids,
                        run_id=result.run_id,
                    )
                return End(
                    TriageResult(decision=decision, target=target, result=result)
                )

            state.decision = decision
            span.set_attribute("triage.action", decision.action)
            span.set_attribute("triage.target_id", source_target.channel_id)
            logfire.info(
                "triage -> {action} target={target_id}",
                action=decision.action,
                target_id=source_target.channel_id,
                reason=decision.reason,
            )
            target = source_target

            reception = ctx.deps.reception(
                source_channel_id, decision.agent_id, decision.model
            )
            model = reception.model
            decision = decision.model_copy(
                update={"agent_id": reception.agent, "model": model}
            )
            state.decision = decision
            if target.mode != "sub":
                target = replace(target, mode="sub")
            state.target = target
            state.claim_handoff = True
            state.handoff_from_agent_tentacle_id = agent.id
            state.run_name = "reception"
            return PrepareReception()


@dataclass
class PrepareReception(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunReception:
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
            raise ValueError("PrepareReception requires a decision and source target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("PrepareReception requires a summon decision")
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

        if target.thread_strategy == "main_only":
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
        return RunReception()


@dataclass
class RunReception(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    resume_batch_id: uuid.UUID | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> PrepareReception | End[TriageGraphResult]:
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
            raise ValueError("RunReception requires a decision and resolved target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("RunReception requires a summon decision")
        target_address = target.address
        source_address = source_target.address
        reception = ctx.deps.reception(
            source_address.channel_tentacle_id, decision.agent_id, decision.model
        )
        model = reception.model
        decision = decision.model_copy(
            update={"agent_id": reception.agent, "model": model}
        )
        state.decision = decision
        agent = ctx.deps.agent(reception.agent)
        reception_model = agent.models.get(model)
        if reception_model is None:
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
        summon = SummonCapability(
            routes=routes,
            current_agent_id=agent.id,
        )

        deferred_results = None
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
            run_name="reception",
            source_address=source_address,
            target_address=target_address,
            target_mode=target.mode,
            decision=state.decision,
            thread_id=thread_id,
            emit_on_stream=target_channel.config.stream.enabled,
        )

        with logfire.span(
            "reception",
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
                        run_name="reception",
                        model=reception_model,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                        capabilities=[summon],
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
                        f"reception stream for {target_address} completed without a result"
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
                    run_name="reception",
                    model=reception_model,
                    deferred_tool_results=deferred_results,
                    deferred_suspender=suspender,
                    capabilities=[summon],
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

            span.set_attribute("reception.run_id", run_result.run_id)
            span.set_attribute(
                "reception.deferred", isinstance(output, DeferredToolRequests)
            )
            if not assistant_replies_bound:
                await ctx.deps.thread_manager.bind_assistant_replies(
                    reply_thread_message_ids,
                    run_id=run_result.run_id,
                )
            if isinstance(output, DeferredToolRequests):
                return End(
                    DeferredResult(
                        requests=output,
                        target=target,
                        run_name="reception",
                        result=run_result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            summon_decision = summon.decision
            if summon_decision is not None:
                state.decision = summon_decision
                state.target = target
                state.claim_handoff = True
                state.handoff_from_agent_tentacle_id = agent.id
                state.run_name = "reception"
                span.set_attribute("reception.action", summon_decision.action)
                span.set_attribute("reception.next_agent_id", summon_decision.agent_id)
                logfire.info(
                    "reception -> {action} agent={agent_id}",
                    action=summon_decision.action,
                    agent_id=summon_decision.agent_id,
                    reason=summon_decision.reason,
                )
                return PrepareReception()

            return End(
                TriageResult(
                    decision=decision,
                    target=target,
                    result=run_result,
                )
            )


@dataclass
class ResumeDeferred(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
    awake: DeferredActionBatchResponse

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunTriage | RunReception | End[TriageGraphResult]:
        state = ctx.state
        with logfire.span("resume_deferred", batch_id=str(self.awake.batch_id)) as span:
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

        if batch.run_name == "triage":
            if batch.status in {"completed", "resuming"} or not batch.completed:
                return End(
                    DeferredResult(
                        requests=batch.requests,
                        target=target,
                        run_name="triage",
                        result=AgentRunResult(batch.requests),
                        batch_id=batch.id,
                    )
                )

            await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
            state.user_prompt = None
            state.run_name = "triage"
            logfire.info("resume routes to RunTriage", batch_id=str(batch.id))
            return RunTriage(resume_batch_id=batch.id)

        if isinstance(batch.decision, (DirectAnswerDecision, SummonDecision)):
            decision = batch.decision
        elif batch.decision:
            decision = TriageDecisionAdapter.validate_python(batch.decision)
        else:
            reception = ctx.deps.reception(
                batch.source_address.channel_tentacle_id,
                batch.agent_tentacle_id,
                None,
            )
            model = reception.model
            decision = SummonDecision(
                action="summon",
                agent_id=reception.agent,
                model=model,
                reason="Resuming deferred human input.",
                hint="Resuming deferred human input.",
                summon="",
            )
        if not isinstance(decision, SummonDecision):
            raise ValueError("reception deferred batch requires a summon decision")
        decision = decision.model_copy(update={"agent_id": batch.agent_tentacle_id})
        state.decision = decision
        if batch.status in {"completed", "resuming"}:
            logfire.info("resume already completed", batch_id=str(batch.id))
            return End(TriageResult(decision=decision, target=target))
        if not batch.completed:
            return End(
                DeferredResult(
                    requests=batch.requests,
                    target=target,
                    run_name="reception",
                    result=AgentRunResult(batch.requests),
                    batch_id=batch.id,
                )
            )

        await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
        state.user_prompt = None
        state.run_name = "reception"
        logfire.info("resume routes to RunReception", batch_id=str(batch.id))
        return RunReception(resume_batch_id=batch.id)


triage_graph = Graph[TriageState, TriageDeps, TriageGraphResult](
    nodes=[
        Awake,
        Route,
        RunTriage,
        PrepareReception,
        RunReception,
        ResumeDeferred,
    ],
    name="triage",
)
