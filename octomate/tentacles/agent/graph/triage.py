from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias

from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.graph.suspender import HumanReviewSuspender
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.output import markdown_from_output

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
    result: AgentRunResult[ChannelOutput] | None = None


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
    message_history: list[ModelMessage] = field(default_factory=list)
    reception_history: list[ModelMessage] | None = None
    deferred_tool_results: DeferredToolResults | None = None
    resume_batch_id: uuid.UUID | None = None
    run_name: Literal["triage", "reception"] = "triage"


@dataclass
class TriageDeps:
    channels: dict[str, ChannelTentacle]
    agents: dict[str, AgentTentacle[ChannelOutput, None]] = field(
        default_factory=dict
    )
    conversation_manager: ConversationManager = field(
        default_factory=ConversationManager
    )
    action_manager: DeferredActionManager = field(default_factory=DeferredActionManager)

    def channel_for(self, target: ResponseTarget) -> ChannelTentacle:
        channel = self.channels.get(target.channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {target.channel_id!r}")
        return channel

    def agent_for(self, agent_id: str) -> AgentTentacle[ChannelOutput, None]:
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

        conversation = await ctx.deps.conversation_manager.ensure(
            key,
            agent_tentacle_id=channel.config.agent_id,
        )
        resolved_agent_id = channel.config.agent_id or conversation.agent_tentacle_id
        if not resolved_agent_id:
            raise ValueError(f"conversation {key} has no agent assigned")
        ctx.deps.agent_for(resolved_agent_id)

        source_target = ResponseTarget(
            channel_id=key.channel_tentacle_id,
            key=key,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
        ctx.state.source_target = source_target
        ctx.state.agent_id = resolved_agent_id
        ctx.state.message_history = list(conversation.messages)

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        ctx.state.user_prompt = user_prompt
        if not user_prompt:
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
            state.decision = TriageDecision(
                action="reception",
                target_id=source_target.channel_id,
                reason="Continuing in the current thread.",
                handoff=str(state.user_prompt or ""),
            )
            state.target = replace(source_target, mode="sub")
            state.run_name = "reception"
            state.reception_history = None
            return RunReception()
        return RunTriage()


@dataclass
class RunTriage(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
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
        result = await agent.run(
            state.user_prompt,
            conversation_key=source_key,
            run_name="triage",
            output_type=[TriageDecision, DeferredToolRequests],
            message_history=state.message_history,
            deferred_tool_results=state.deferred_tool_results,
            deferred_suspender=suspender,
            instructions=TRIAGE_INSTRUCTIONS.format(
                targets="\n".join(str(target) for target in candidates.values())
            ),
        )
        if state.resume_batch_id is not None:
            await ctx.deps.action_manager.mark_batch(
                state.resume_batch_id,
                "completed",
                completed=True,
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
        target = candidates.get(decision.target_id) or source_target

        if decision.action == "answer":
            if target.mode != "main":
                target = replace(target, mode="main")
            state.target = target
            if target.key is None:
                raise ValueError(f"target {target.channel_id!r} has no resolved key")
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
        if target.key == source_key and target.mode == "main":
            state.reception_history = list(state.message_history)
        else:
            state.reception_history = None
        return RunReception()


@dataclass
class RunReception(BaseNode[TriageState, TriageDeps, TriageGraphResult]):
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

        if state.reception_history is not None:
            message_history = state.reception_history
        else:
            conversation = await ctx.deps.conversation_manager.ensure(
                target_key,
                agent_tentacle_id=agent.id,
            )
            message_history = list(conversation.messages)

        if state.deferred_tool_results is not None:
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
        )

        result: AgentRunResult[ChannelOutput] | None = None
        if target_channel.config.stream.enabled:

            async def stream_events() -> AsyncIterator[
                AgentStreamEvent | AgentRunResultEvent[ChannelOutput]
            ]:
                nonlocal result
                async with agent.run_stream_events(
                    user_prompt,
                    conversation_key=target_key,
                    run_name="reception",
                    message_history=message_history,
                    deferred_tool_results=state.deferred_tool_results,
                    deferred_suspender=suspender,
                ) as stream:
                    async for event in stream:
                        if isinstance(event, AgentRunResultEvent):
                            result = event.result
                        yield event

            await target_channel.feelers.event_stream.present(
                target_key,
                stream_events(),
            )
            if result is None:
                raise RuntimeError(
                    f"reception stream for {target_key} completed without a result"
                )
        else:
            result = await agent.run(
                user_prompt,
                conversation_key=target_key,
                run_name="reception",
                message_history=message_history,
                deferred_tool_results=state.deferred_tool_results,
                deferred_suspender=suspender,
            )
            if not isinstance(result.output, DeferredToolRequests):
                markdown = markdown_from_output(result.output)
                if markdown is not None:
                    await target_channel.feelers.markdown.present(
                        target_key,
                        markdown,
                    )

        if state.resume_batch_id is not None:
            await ctx.deps.action_manager.mark_batch(
                state.resume_batch_id,
                "completed",
                completed=True,
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
        batch = await ctx.deps.action_manager.resolve_batch(self.awake)
        target_channel = ctx.deps.channels.get(batch.target_key.channel_tentacle_id)
        if target_channel is None:
            raise ValueError(
                f"unknown channel {batch.target_key.channel_tentacle_id!r}"
            )
        agent = ctx.deps.agent_for(batch.agent_tentacle_id)
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
            conversation = await ctx.deps.conversation_manager.ensure(
                batch.target_key,
                agent_tentacle_id=agent.id,
            )
            state.user_prompt = None
            state.message_history = list(conversation.messages)
            state.deferred_tool_results = batch.build_results()
            state.resume_batch_id = batch.id
            state.run_name = "triage"
            return RunTriage()

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
        state.deferred_tool_results = batch.build_results()
        state.resume_batch_id = batch.id
        state.run_name = "reception"
        state.reception_history = None
        return RunReception()


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
