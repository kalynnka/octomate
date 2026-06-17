from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, TypeAlias

import logfire
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests
# TODO: migrate this graph to the pydantic_graph GraphBuilder (Step/Decision/Edge)
# API once pydantic-graph v2 is officially released. The BaseNode `Graph` runner is
# deprecated for v2; pinned <2 in pyproject.toml until then.
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.capabilities.events import StreamEvents
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.agent.inkling.graph.suspender import HumanReviewSuspender
from octomate.tentacles.channel.base import (
    ChannelTentacle,
    ThreadStrategy,
)

logger = logging.getLogger(__name__)

TRIAGE_INSTRUCTIONS = """\
Decide how Octomate should respond to the latest user message.

Use action="answer" for simple Q&A that can be answered directly.
Use action="reception" for multi-step work, debugging, tool-heavy work, long-running operations, or anything that should continue away from the main chat.

Available response targets:
{targets}

Return target_id as one of the available target ids. If unsure, leave target_id empty.
For action="answer", put the complete user-facing reply in answer.
For action="reception", keep answer empty, put a short user-facing thread starter
message in hint, explain the routing in reason, and put the complete reception
handoff prompt in handoff. Include the user's request in handoff so the reception
run can start from it without reading the triage model output.
"""


@dataclass(frozen=True)
class ResponseTarget:
    channel_id: str
    key: ConversationKey | None = None
    thread_strategy: ThreadStrategy = "main_only"
    mode: ResponseTargetMode = "main"

    def __str__(self) -> str:
        chat_type = self.key.chat_type if self.key else "unresolved"
        return (
            f"- {self.channel_id}: chat_type={chat_type}, mode={self.mode}, "
            f"thread_strategy={self.thread_strategy}"
        )


@dataclass
class TriageResult:
    decision: TriageDecision
    target: ResponseTarget
    result: AgentRunResult[InklingOutput] | None = None


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
    read from state and carry only transition discriminators. The agent travels
    as `agent_id` (resolved via `TriageDeps.agent_for`) so the state stays free
    of live tentacle objects.
    """

    source_target: ResponseTarget | None = None
    target: ResponseTarget | None = None
    agent_id: str | None = None
    decision: TriageDecision | None = None
    user_prompt: str | Sequence[UserContent] | None = None
    run_name: Literal["triage", "reception"] = "triage"


@dataclass
class TriageDeps:
    channels: dict[str, ChannelTentacle]
    agents: dict[str, AgentTentacle[InklingOutput, None]] = field(default_factory=dict)
    conversation_manager: ConversationManager = field(
        default_factory=ConversationManager
    )
    action_manager: DeferredActionManager = field(default_factory=DeferredActionManager)

    def channel_for(self, target: ResponseTarget) -> ChannelTentacle:
        channel = self.channels.get(target.channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {target.channel_id!r}")
        return channel

    def agent_for(self, agent_id: str) -> AgentTentacle[InklingOutput, None]:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"unknown agent {agent_id!r}")
        return agent


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
                    decision=TriageDecision(
                        action="answer",
                        reason="Empty awake signal.",
                    ),
                    target=ResponseTarget(channel_id=""),
                )
            )

        key = self.signal.key
        channel = ctx.deps.channels.get(key.channel_tentacle_id)
        if channel is None:
            raise ValueError(f"unknown channel {key.channel_tentacle_id!r}")

        resolved_agent_id = channel.config.agent_id
        ctx.deps.agent_for(resolved_agent_id)
        await ctx.deps.conversation_manager.ensure(
            key,
            agent_tentacle_id=resolved_agent_id,
        )

        source_target = ResponseTarget(
            channel_id=key.channel_tentacle_id,
            key=key,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
        ctx.state.source_target = source_target
        ctx.state.agent_id = resolved_agent_id

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        ctx.state.user_prompt = user_prompt
        if not user_prompt:
            logfire.info(
                "awake short-circuit: empty prompt",
                channel_id=key.channel_tentacle_id,
                conversation_key=str(key),
            )
            return End(
                TriageResult(
                    decision=TriageDecision(
                        action="answer",
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
        if source_target is None or source_target.key is None:
            raise ValueError("Route requires a resolved source target")
        source_key = source_target.key

        if source_key.thread_id and source_target.thread_strategy == "flat_thread":
            logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_key.channel_tentacle_id,
                conversation_key=str(source_key),
            )
            state.decision = TriageDecision(
                action="reception",
                target_id=source_target.channel_id,
                reason="Continuing in the current thread.",
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
        agent_id = state.agent_id
        if source_target is None or source_target.key is None or agent_id is None:
            raise ValueError("RunTriage requires a resolved source target and agent")
        source_key = source_target.key
        agent = ctx.deps.agent_for(agent_id)

        candidates = {
            channel_id: ResponseTarget(
                channel_id=channel_id,
                key=source_key if channel_id == source_target.channel_id else None,
                thread_strategy=channel.thread_strategy,
                mode="main",
            )
            for channel_id, channel in ctx.deps.channels.items()
        }
        candidates[source_target.channel_id] = source_target

        suspender = HumanReviewSuspender(
            channel=ctx.deps.channel_for(source_target),
            action_manager=ctx.deps.action_manager,
            conversation_manager=ctx.deps.conversation_manager,
            agent_tentacle_id=agent.id,
            run_name="triage",
            source_key=source_key,
            target_key=source_key,
            target_mode="main",
            decision=None,
        )
        deferred_results = None
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        with logfire.span(
            "triage",
            channel_id=source_key.channel_tentacle_id,
            agent_id=agent.id,
            conversation_key=str(source_key),
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            result = await agent.run(
                state.user_prompt,
                conversation_key=source_key,
                run_name="triage",
                output_type=[TriageDecision, DeferredToolRequests],
                deferred_tool_results=deferred_results,
                deferred_suspender=suspender,
                instructions=TRIAGE_INSTRUCTIONS.format(
                    targets="\n".join(str(target) for target in candidates.values())
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
            decision = (
                output
                if isinstance(output, TriageDecision)
                else TriageDecision.model_validate(output)
            )
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

            if decision.action == "answer":
                if target.mode != "main":
                    target = replace(target, mode="main")
                state.target = target
                if target.key is None:
                    raise ValueError(
                        f"target {target.channel_id!r} has no resolved key"
                    )
                await ctx.deps.channel_for(target).feelers.markdown.present(
                    target.key,
                    decision.answer or decision.hint,
                )
                return End(TriageResult(decision=decision, target=target))

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
            or source_target.key is None
        ):
            raise ValueError("PrepareReception requires a decision and source target")
        source_key = source_target.key
        channel = ctx.deps.channel_for(target)

        target_key = target.key
        if target_key is None:
            target_key = ConversationKey(
                channel_tentacle_id=target.channel_id,
                chat_type=source_key.chat_type,
                chat_id=source_key.chat_id,
                user_id=source_key.user_id,
                thread_id="",
            )
            target = replace(target, key=target_key)

        if target.thread_strategy == "main_only":
            target = replace(target, mode="main")
        elif not target_key.thread_id:
            try:
                target_key = await channel.start_sub_thread(
                    target_key,
                    decision.hint
                    or decision.reason
                    or "Octomate is continuing this request here.",
                )
                target = replace(target, key=target_key)
            except Exception:
                logger.warning(
                    "Channel %s failed to start a sub-thread; using main target",
                    target.channel_id,
                    exc_info=True,
                )
                target = replace(target, mode="main")
        else:
            target = replace(target, key=target_key)

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
        agent_id = state.agent_id
        if (
            decision is None
            or target is None
            or target.key is None
            or source_target is None
            or source_target.key is None
            or agent_id is None
        ):
            raise ValueError("RunReception requires a decision and resolved target")
        target_key = target.key
        source_key = source_target.key
        agent = ctx.deps.agent_for(agent_id)
        target_channel = ctx.deps.channel_for(target)

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
            source_key=source_key,
            target_key=target_key,
            target_mode=target.mode,
            decision=state.decision,
            emit_on_stream=target_channel.config.stream.enabled,
        )

        with logfire.span(
            "reception",
            channel_id=target_key.channel_tentacle_id,
            agent_id=agent.id,
            conversation_key=str(target_key),
            streaming=target_channel.config.stream.enabled,
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            result: AgentRunResult[InklingOutput] | None = None
            if target_channel.config.stream.enabled:

                async def stream_events() -> AsyncIterator[
                    StreamEvents[InklingOutput] | AgentRunResultEvent[InklingOutput]
                ]:
                    nonlocal result
                    async with agent.run_stream_events(
                        user_prompt,
                        conversation_key=target_key,
                        run_name="reception",
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                result = event.result
                            yield event

                async with target_channel.feelers.timeline.open(target_key) as state:
                    await state.drive(stream_events())
                if result is None:
                    raise RuntimeError(
                        f"reception stream for {target_key} completed without a result"
                    )
            else:
                result = await agent.run(
                    user_prompt,
                    conversation_key=target_key,
                    run_name="reception",
                    deferred_tool_results=deferred_results,
                    deferred_suspender=suspender,
                )
                output = result.output
                if isinstance(output, str):
                    if output:
                        await target_channel.feelers.markdown.present(
                            target_key,
                            output,
                        )
                elif isinstance(output, Iterable):
                    # A segment list is the only media-bearing reply: deliver it
                    # natively (channels without a media transport fall back to
                    # the joined text form in send_segments).
                    await target_channel.feelers.segments.present(
                        target_key, list(output)
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
        target_channel = ctx.deps.channels.get(batch.target_key.channel_tentacle_id)
        if target_channel is None:
            raise ValueError(
                f"unknown channel {batch.target_key.channel_tentacle_id!r}"
            )
        ctx.deps.agent_for(batch.agent_tentacle_id)
        target = ResponseTarget(
            channel_id=batch.target_key.channel_tentacle_id,
            key=batch.target_key,
            thread_strategy=target_channel.thread_strategy,
            mode=batch.target_mode,
        )
        source_channel = ctx.deps.channels.get(batch.source_key.channel_tentacle_id)
        state.agent_id = batch.agent_tentacle_id
        state.target = target
        state.source_target = ResponseTarget(
            channel_id=batch.source_key.channel_tentacle_id,
            key=batch.source_key,
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

        if isinstance(batch.decision, TriageDecision):
            decision = batch.decision
        elif batch.decision:
            decision = TriageDecision.model_validate(batch.decision)
        else:
            decision = TriageDecision(
                action="reception",
                target_id=batch.target_key.channel_tentacle_id,
                reason="Resuming deferred human input.",
            )
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
