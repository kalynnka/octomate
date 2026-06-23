from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, TypeAlias, overload

import logfire
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.tools import DeferredToolRequests

# TODO: migrate this graph to the pydantic_graph GraphBuilder (Step/Decision/Edge)
# API once pydantic-graph v2 is officially released. The BaseNode `Graph` runner is
# deprecated for v2; pinned <2 in pyproject.toml until then.
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.capabilities.events import StreamEvents
from octomate.config.channels import AgentModelConfig
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import (
    DirectAnswerDecision,
    HandoffDecision,
    ResponseTargetMode,
    TriageDecision,
    TriageDecisionAdapter,
)
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import (
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.triage.suspender import HumanReviewSuspender

logger = logging.getLogger(__name__)

# The output a reception agent returns and the graph delivers, independent of any
# specific agent: plain text, a media segment list, or a deferred-tool request.
TriageOutput: TypeAlias = str | list[MessageSegment] | DeferredToolRequests

TRIAGE_INSTRUCTIONS = """\
Decide how Octomate should respond to the latest user message.

Use action="direct_answer" for simple Q&A you can answer directly and completely.
Use action="handoff" for multi-step work, debugging, coding, tool-heavy or
long-running operations, or anything better handled by a specialist away from
the main chat.

Available response targets (where the reply is delivered):
{targets}

Available reception routes (who does the work and which model to use):
{agents}

For action="direct_answer": set `target_id` to a response target id and put the
complete user-facing reply in `answer`.

For action="handoff":
- Set `agent_id` and `model` from one listed reception route. Leave `model`
  empty only when that route says "default".
- Set `target_id` to a response target id (default to the source channel).
- Put a short user-facing thread starter in `hint` and the routing rationale in
  `reason`.
- Put a DETAILED, self-contained handoff in `handoff`: the reception agent never
  sees this chat, so include the user's full request, relevant context,
  constraints, acceptance criteria, and anything it needs to start working
  without re-reading prior messages.
"""


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
    result: AgentRunResult[TriageOutput] | None = None


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
    user_prompt: str | Sequence[UserContent] | None = None
    run_name: Literal["triage", "reception"] = "triage"


@dataclass
class TriageDeps:
    channels: dict[str, ChannelTentacle]
    agents: dict[str, AgentTentacle[TriageOutput, None]] = field(default_factory=dict)
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

    def agent(self, agent_id: str) -> AgentTentacle[TriageOutput, None]:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"unknown agent {agent_id!r}")
        return agent

    def triage(self, channel_id: str) -> AgentModelConfig:
        return self.channel(channel_id).config.triage

    def receptions(self, channel_id: str) -> list[AgentModelConfig]:
        return self.channel(channel_id).config.receptions

    def reception(
        self,
        channel_id: str,
        agent_id: str,
        model: str,
    ) -> AgentModelConfig:
        receptions = self.receptions(channel_id)
        if not receptions:
            raise ValueError(f"channel {channel_id!r} has no reception agents")
        for reception in receptions:
            if reception.agent == agent_id and (reception.model or "") == model:
                return reception
        if agent_id:
            for reception in receptions:
                if reception.agent == agent_id:
                    return reception
        return receptions[0]

    def model(self, config: AgentModelConfig) -> Model | str | None:
        return self.agent(config.agent).model_for(config.model)


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
            state.decision = HandoffDecision(
                action="handoff",
                target_id=source_target.channel_id,
                agent_id=reception.agent,
                model=reception.model or "",
                reason="Continuing in the current thread.",
                hint="Continuing in the current thread.",
                handoff=str(state.user_prompt or ""),
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
        receptions = ctx.deps.receptions(source_channel_id)
        triage_model = ctx.deps.model(triage)

        candidates = {
            channel_id: ResponseTarget(
                channel_id=channel_id,
                address=source_address
                if channel_id == source_target.channel_id
                else None,
                thread_strategy=channel.thread_strategy,
                mode="main",
            )
            for channel_id, channel in ctx.deps.channels.items()
        }
        candidates[source_target.channel_id] = source_target

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
                model=triage_model,
                output_type=(
                    TriageDecision
                    if agent.in_process
                    else [TriageDecision, DeferredToolRequests]
                ),
                deferred_tool_results=deferred_results,
                deferred_suspender=suspender,
                instructions=TRIAGE_INSTRUCTIONS.format(
                    targets="\n".join(str(target) for target in candidates.values()),
                    agents="\n".join(
                        (
                            f"- {reception.agent} "
                            f"(model: {reception.model or 'default'}): "
                            f"{ctx.deps.agent(reception.agent).description}"
                        )
                        for reception in receptions
                    ),
                ),
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
            if not isinstance(output, (DirectAnswerDecision, HandoffDecision)):
                raise TypeError(
                    f"triage agent returned unsupported output {type(output).__name__}"
                )
            decision: TriageDecision = output
            state.decision = decision
            span.set_attribute("triage.action", decision.action)
            span.set_attribute("triage.target_id", decision.target_id)
            logfire.info(
                "triage -> {action} target={target_id}",
                action=decision.action,
                target_id=decision.target_id,
                reason=decision.reason,
            )
            target = candidates.get(decision.target_id) or source_target

            if isinstance(decision, DirectAnswerDecision):
                if target.mode != "main":
                    target = replace(target, mode="main")
                state.target = target
                if target.address is None:
                    raise ValueError(
                        f"target {target.channel_id!r} has no resolved address"
                    )
                await ctx.deps.channel(target).feelers.markdown.present(
                    target.address,
                    decision.answer,
                )
                return End(TriageResult(decision=decision, target=target))

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
        if not isinstance(decision, HandoffDecision):
            raise ValueError("PrepareReception requires a handoff decision")
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
    ) -> End[TriageGraphResult]:
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
        if not isinstance(decision, HandoffDecision):
            raise ValueError("RunReception requires a handoff decision")
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
        reception_model = ctx.deps.model(reception)
        target_channel = ctx.deps.channel(target)

        deferred_results = None
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        if deferred_results is not None:
            user_prompt: str | Sequence[UserContent] | None = None
        else:
            user_prompt = decision.handoff or str(state.user_prompt or "")

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
            result: AgentRunResult[TriageOutput] | None = None
            if target_channel.config.stream.enabled:

                async def stream_events() -> AsyncIterator[
                    StreamEvents[TriageOutput] | AgentRunResultEvent[TriageOutput]
                ]:
                    nonlocal result
                    async with agent.run_stream_events(
                        user_prompt,
                        conversation_address=target_address,
                        run_name="reception",
                        model=reception_model,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                result = event.result
                            yield event

                async with target_channel.feelers.timeline.open(
                    target_address
                ) as state:
                    await state.drive(stream_events())
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
                "reception.deferred", isinstance(result.output, DeferredToolRequests)
            )
            if isinstance(result.output, DeferredToolRequests):
                return End(
                    DeferredResult(
                        requests=result.output,
                        target=target,
                        run_name="reception",
                        result=result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )
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

        if isinstance(batch.decision, (DirectAnswerDecision, HandoffDecision)):
            decision = batch.decision
        elif batch.decision:
            decision = TriageDecisionAdapter.validate_python(batch.decision)
        else:
            decision = HandoffDecision(
                action="handoff",
                target_id=batch.target_address.channel_tentacle_id,
                agent_id=batch.agent_tentacle_id,
                model="",
                reason="Resuming deferred human input.",
                hint="Resuming deferred human input.",
                handoff="",
            )
        if not isinstance(decision, HandoffDecision):
            raise ValueError("reception deferred batch requires a handoff decision")
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
