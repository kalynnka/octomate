from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Iterable, Literal, TypeAlias, cast, overload

import logfire
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import UserContent
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests

# TODO: migrate this graph to the pydantic_graph GraphBuilder (Step/Decision/Edge)
# API once pydantic-graph v2 is officially released. The BaseNode `Graph` runner is
# deprecated for v2; pinned <2 in pyproject.toml until then.
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.capabilities.events import StreamEvents
from octomate.capabilities.summon import SummonCapability
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
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
    user_prompt: str | Sequence[UserContent] | None = None
    run_name: Literal["triage", "reception"] = "triage"


@dataclass
class TriageDeps:
    channels: dict[str, ChannelTentacle]
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    conversation_manager: ConversationManager = field(
        default_factory=ConversationManager
    )
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
        for channel_id, channel in self.channels.items():
            routes: list[SummonRoute] = []
            for reception in self.receptions(channel_id):
                agent = self.agent(reception.agent)
                routes.append(
                    SummonRoute(
                        agent_id=reception.agent,
                        model=reception.model or "",
                        description=agent.description,
                    )
                )
            available[channel_id] = routes
        return available

    def reception(
        self,
        channel_id: str,
        agent_id: str,
        model: str,
    ) -> AgentModelConfig:
        receptions = self.receptions(channel_id)
        matched: AgentModelConfig | None = None
        for reception in receptions:
            if reception.agent == agent_id and (reception.model or "") == model:
                return reception
            if agent_id and reception.agent == agent_id:
                matched = reception
        return matched or receptions[0]


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

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        ctx.state.user_prompt = user_prompt
        if not user_prompt:
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

        if source_address.thread_id and source_target.thread_strategy == "flat_thread":
            logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_address.channel_tentacle_id,
                conversation_address=str(source_address),
            )
            reception = ctx.deps.reception(source_address.channel_tentacle_id, "", "")
            state.decision = SummonDecision(
                action="summon",
                agent_id=reception.agent,
                model=reception.model or "",
                reason="Continuing in the current thread.",
                hint="Continuing in the current thread.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(source_target, mode="sub")
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
        triage_model = None
        if triage.model:
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
                if isinstance(output, list):
                    await ctx.deps.channel(target).feelers.segments.present(
                        target.address, output
                    )
                elif answer:
                    await ctx.deps.channel(target).feelers.markdown.present(
                        target.address,
                        answer,
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
            decision = decision.model_copy(
                update={"agent_id": reception.agent, "model": reception.model or ""}
            )
            state.decision = decision
            if target.mode != "sub":
                target = replace(target, mode="sub")
            state.target = target
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
        decision = decision.model_copy(
            update={"agent_id": reception.agent, "model": reception.model or ""}
        )
        state.decision = decision
        agent = ctx.deps.agent(reception.agent)
        reception_model = None
        if reception.model:
            reception_model = agent.models.get(reception.model)
            if reception_model is None:
                raise ValueError(
                    f"agent {agent.id!r} has no configured model {reception.model!r}"
                )
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
            result: AgentRunResult[ChannelOutput] | None = None
            if target_channel.config.stream.enabled:

                async def stream_events() -> AsyncIterator[
                    StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
                ]:
                    nonlocal result
                    async with agent.run_stream_events(
                        user_prompt,
                        conversation_address=target_address,
                        run_name="reception",
                        model=reception_model,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                        capabilities=[summon],
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                result = event.result
                            yield event

                async with target_channel.feelers.timeline.open(
                    target_address
                ) as timeline_state:
                    await timeline_state.drive(stream_events())
                if result is None:
                    raise RuntimeError(
                        f"reception stream for {target_address} completed without a result"
                    )
            else:
                result = await agent.run(
                    user_prompt,
                    conversation_address=target_address,
                    run_name="reception",
                    model=reception_model,
                    deferred_tool_results=deferred_results,
                    deferred_suspender=suspender,
                    capabilities=[summon],
                )
                output = result.output
                if isinstance(output, str):
                    if output:
                        await target_channel.feelers.markdown.present(
                            target_address,
                            output,
                        )
                elif isinstance(output, Iterable):
                    # A segment list is the only media-bearing reply: deliver it
                    # natively (channels without a media transport fall back to
                    # the joined text form in send_segments).
                    await target_channel.feelers.segments.present(
                        target_address, list(output)
                    )
                # DeferredToolRequests / None: nothing to deliver here.

            if self.resume_batch_id is not None:
                await ctx.deps.action_manager.mark_batch(
                    self.resume_batch_id,
                    "completed",
                    completed=True,
                )

            span.set_attribute("reception.run_id", result.run_id)
            span.set_attribute(
                "reception.deferred", isinstance(output, DeferredToolRequests)
            )
            if isinstance(output, DeferredToolRequests):
                return End(
                    DeferredResult(
                        requests=output,
                        target=target,
                        run_name="reception",
                        result=result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            summon_decision = summon.decision
            if summon_decision is not None:
                state.decision = summon_decision
                state.target = target
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
                    result=result,
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
            decision = SummonDecision(
                action="summon",
                agent_id=batch.agent_tentacle_id,
                model="",
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
