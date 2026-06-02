from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, TypeAlias

from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import (
    AwakeSignal,
    DeferredActionBatchResponse,
)
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.agent.base import AgentTentacle
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
    result: AgentRunResult[DeferredToolRequests]


TriageGraphResult: TypeAlias = TriageResult | DeferredResult


@dataclass
class TriageState:
    pass


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


@dataclass
class Awake(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    signal: AwakeSignal

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunTriage | ResumeDeferredActions | End[TriageGraphResult]:
        if isinstance(self.signal, DeferredActionBatchResponse):
            batch = await ctx.deps.action_manager.get_batch(self.signal.batch_id)
            source_key = batch.source_key
            target_key = batch.target_key
            source_channel = ctx.deps.channels.get(source_key.channel_tentacle_id)
            target_channel = ctx.deps.channels.get(target_key.channel_tentacle_id)
            if source_channel is None:
                raise ValueError(f"unknown channel {source_key.channel_tentacle_id!r}")
            if target_channel is None:
                raise ValueError(f"unknown channel {target_key.channel_tentacle_id!r}")

            agent_tentacle = ctx.deps.agents.get(batch.agent_tentacle_id)
            if agent_tentacle is None:
                raise ValueError(f"unknown agent {batch.agent_tentacle_id!r}")

            source_target = ResponseTarget(
                channel_id=source_key.channel_tentacle_id,
                key=source_key,
                thread_strategy=source_channel.thread_strategy,
                mode="main",
            )
            return ResumeDeferredActions(
                awake=self.signal,
                agent=agent_tentacle,
                source_target=source_target,
            )

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
        agent_tentacle = ctx.deps.agents.get(resolved_agent_id)
        if agent_tentacle is None:
            raise ValueError(f"unknown agent {resolved_agent_id!r}")

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        source_target = ResponseTarget(
            channel_id=key.channel_tentacle_id,
            key=key,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
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
        return RunTriage(
            agent=agent_tentacle,
            source_target=source_target,
            user_prompt=user_prompt,
            message_history=list(conversation.messages),
        )


@dataclass
class RunTriage(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    agent: AgentTentacle[ChannelOutput, None]
    source_target: ResponseTarget
    user_prompt: str | Sequence[UserContent] | None
    message_history: list[ModelMessage] = field(default_factory=list)
    deferred_tool_results: DeferredToolResults | None = None
    deferred_batch_id: uuid.UUID | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RequestDeferredActions | AnswerDirect | PrepareReception | RunReception:
        source_target = self.source_target
        if source_target.key is None:
            raise ValueError("RunTriage.source_target requires a ConversationKey")
        source_key = source_target.key
        targets = {
            channel_id: ResponseTarget(
                channel_id=channel_id,
                key=source_key if channel_id == source_target.channel_id else None,
                thread_strategy=channel.thread_strategy,
                mode="main",
            )
            for channel_id, channel in ctx.deps.channels.items()
        }
        targets[source_target.channel_id] = source_target
        if source_key.thread_id and source_target.thread_strategy == "flat_thread":
            return RunReception(
                agent=self.agent,
                source_key=source_key,
                user_prompt=self.user_prompt,
                decision=TriageDecision(
                    action="reception",
                    target_id=source_target.channel_id,
                    reason="Continuing in the current thread.",
                    handoff=str(self.user_prompt or ""),
                ),
                target=replace(source_target, mode="sub"),
                target_key=source_key,
            )

        result = await self.agent.run(
            self.user_prompt,
            conversation_key=source_key,
            run_name="triage",
            output_type=[TriageDecision, DeferredToolRequests],
            message_history=self.message_history,
            deferred_tool_results=self.deferred_tool_results,
            instructions=TRIAGE_INSTRUCTIONS.format(
                targets="\n".join(str(target) for target in targets.values())
            ),
        )
        if self.deferred_batch_id is not None:
            await ctx.deps.action_manager.mark_batch(
                self.deferred_batch_id,
                "completed",
                completed=True,
            )

        if isinstance(result.output, DeferredToolRequests):
            return RequestDeferredActions(
                agent=self.agent,
                source_key=source_key,
                requests=result.output,
                decision=None,
                target=source_target,
                target_key=source_key,
                run_name="triage",
            )

        output = result.output
        decision = (
            output
            if isinstance(output, TriageDecision)
            else TriageDecision.model_validate(output)
        )
        target = targets.get(decision.target_id) or source_target
        if decision.action == "answer" and target.mode != "main":
            target = replace(target, mode="main")
        elif decision.action == "reception" and target.mode != "sub":
            target = replace(target, mode="sub")

        if decision.action == "answer":
            return AnswerDirect(decision=decision, target=target)
        return PrepareReception(
            agent=self.agent,
            source_key=source_key,
            user_prompt=self.user_prompt,
            decision=decision,
            target=target,
            source_message_history=list(self.message_history),
        )


@dataclass
class AnswerDirect(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    decision: TriageDecision
    target: ResponseTarget

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> End[TriageGraphResult]:
        target_channel = ctx.deps.channel_for(self.target)
        if self.target.key is None:
            raise ValueError(f"target {self.target.channel_id!r} has no resolved key")
        markdown = self.decision.answer or self.decision.reason
        await target_channel.feelers.markdown.present(
            self.target.key,
            markdown,
        )
        return End(TriageResult(decision=self.decision, target=self.target))


@dataclass
class PrepareReception(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    agent: AgentTentacle[ChannelOutput, None]
    source_key: ConversationKey
    user_prompt: str | Sequence[UserContent] | None
    decision: TriageDecision
    target: ResponseTarget
    source_message_history: list[ModelMessage] = field(default_factory=list)

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunReception:
        target_channel = ctx.deps.channel_for(self.target)
        target = self.target
        target_key = target.key
        if target_key is None:
            target_key = ConversationKey(
                channel_tentacle_id=target.channel_id,
                chat_type=self.source_key.chat_type,
                chat_id=self.source_key.chat_id,
                user_id=self.source_key.user_id,
                thread_id="",
            )
            target = replace(target, key=target_key)

        if target.thread_strategy == "main_only":
            target = replace(target, mode="main")
        elif not target_key.thread_id:
            try:
                target_key = await target_channel.start_sub_thread(
                    target_key,
                    self.decision.hint
                    or self.decision.reason
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

        return RunReception(
            agent=self.agent,
            source_key=self.source_key,
            user_prompt=self.decision.handoff or str(self.user_prompt or ""),
            decision=self.decision,
            target=target,
            target_key=target_key,
            message_history=(
                list(self.source_message_history)
                if target_key == self.source_key and target.mode == "main"
                else None
            ),
        )


@dataclass
class RunReception(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    agent: AgentTentacle[ChannelOutput, None]
    source_key: ConversationKey
    user_prompt: str | Sequence[UserContent] | None
    decision: TriageDecision
    target: ResponseTarget
    target_key: ConversationKey
    message_history: list[ModelMessage] | None = None
    deferred_tool_results: DeferredToolResults | None = None
    deferred_batch_id: uuid.UUID | None = None
    result: AgentRunResult[ChannelOutput] | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RequestDeferredActions | End[TriageGraphResult]:
        target_channel = ctx.deps.channel_for(self.target)
        if self.message_history is not None:
            message_history = self.message_history
        else:
            conversation = await ctx.deps.conversation_manager.ensure(
                self.target_key,
                agent_tentacle_id=self.agent.id,
            )
            message_history = list(conversation.messages)

        if target_channel.config.stream.enabled:

            async def stream_events() -> AsyncIterator[
                AgentStreamEvent | AgentRunResultEvent[ChannelOutput]
            ]:
                async with self.agent.run_stream_events(
                    self.user_prompt,
                    conversation_key=self.target_key,
                    run_name="reception",
                    message_history=message_history,
                    deferred_tool_results=self.deferred_tool_results,
                ) as stream:
                    async for event in stream:
                        if isinstance(event, AgentRunResultEvent):
                            self.result = event.result
                        yield event

            await target_channel.feelers.event_stream.present(
                self.target_key,
                stream_events(),
            )
            if self.result is None:
                raise RuntimeError(
                    f"reception stream for {self.target_key} completed without a result"
                )
        else:
            self.result = await self.agent.run(
                self.user_prompt,
                conversation_key=self.target_key,
                run_name="reception",
                message_history=message_history,
                deferred_tool_results=self.deferred_tool_results,
            )

            if not isinstance(self.result.output, DeferredToolRequests):
                markdown = markdown_from_output(self.result.output)
                if markdown is not None:
                    await target_channel.feelers.markdown.present(
                        self.target_key,
                        markdown,
                    )
            if self.result is None:
                raise RuntimeError(
                    f"reception run for {self.target_key} completed without a result"
                )

        if self.deferred_batch_id is not None:
            await ctx.deps.action_manager.mark_batch(
                self.deferred_batch_id,
                "completed",
                completed=True,
            )

        if isinstance(self.result.output, DeferredToolRequests):
            return RequestDeferredActions(
                agent=self.agent,
                source_key=self.source_key,
                requests=self.result.output,
                decision=self.decision,
                target=self.target,
                target_key=self.target_key,
                run_name="reception",
            )

        return End(
            TriageResult(
                decision=self.decision,
                target=self.target,
                result=self.result,
            )
        )


@dataclass
class RequestDeferredActions(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    agent: AgentTentacle[ChannelOutput, None]
    source_key: ConversationKey
    requests: DeferredToolRequests
    decision: TriageDecision | None
    target: ResponseTarget
    target_key: ConversationKey
    run_name: Literal["triage", "reception"]

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> End[TriageGraphResult]:
        target_channel = ctx.deps.channel_for(self.target)
        conversation = await ctx.deps.conversation_manager.ensure(
            self.target_key,
            agent_tentacle_id=self.agent.id,
        )
        await target_channel.feelers.present_actions(
            action_manager=ctx.deps.action_manager,
            conversation=conversation,
            agent_tentacle_id=self.agent.id,
            run_name=self.run_name,
            source_key=self.source_key,
            target_key=self.target_key,
            target_mode=self.target.mode,
            decision=self.decision,
            requests=self.requests,
        )

        return End(
            DeferredResult(
                requests=self.requests,
                target=self.target,
                run_name=self.run_name,
                result=AgentRunResult(self.requests),
            )
        )


@dataclass
class ResumeDeferredActions(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    awake: DeferredActionBatchResponse
    agent: AgentTentacle[ChannelOutput, None]
    source_target: ResponseTarget

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunTriage | RunReception | End[TriageGraphResult]:
        batch = await ctx.deps.action_manager.resolve_batch(self.awake)
        target_channel = ctx.deps.channels.get(batch.target_key.channel_tentacle_id)
        if target_channel is None:
            raise ValueError(
                f"unknown channel {batch.target_key.channel_tentacle_id!r}"
            )
        target = ResponseTarget(
            channel_id=batch.target_key.channel_tentacle_id,
            key=batch.target_key,
            thread_strategy=target_channel.thread_strategy,
            mode=batch.target_mode,
        )

        if batch.run_name == "triage":
            if batch.status in {"completed", "resuming"} or not batch.completed:
                return End(
                    DeferredResult(
                        requests=batch.requests,
                        target=target,
                        run_name="triage",
                        result=AgentRunResult(batch.requests),
                    )
                )

            await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
            conversation = await ctx.deps.conversation_manager.ensure(
                batch.target_key,
                agent_tentacle_id=self.agent.id,
            )
            return RunTriage(
                agent=self.agent,
                source_target=target,
                user_prompt=None,
                message_history=list(conversation.messages),
                deferred_tool_results=batch.build_results(),
                deferred_batch_id=batch.id,
            )

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
        if batch.status in {"completed", "resuming"}:
            return End(TriageResult(decision=decision, target=target))
        if not batch.completed:
            return End(
                DeferredResult(
                    requests=batch.requests,
                    target=target,
                    run_name="reception",
                    result=AgentRunResult(batch.requests),
                )
            )

        await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
        return RunReception(
            agent=self.agent,
            source_key=batch.source_key,
            user_prompt=None,
            decision=decision,
            target=target,
            target_key=batch.target_key,
            deferred_tool_results=batch.build_results(),
            deferred_batch_id=batch.id,
        )


triage_graph = Graph[TriageState, TriageDeps, TriageGraphResult](
    nodes=[
        Awake,
        RunTriage,
        AnswerDirect,
        PrepareReception,
        RunReception,
        RequestDeferredActions,
        ResumeDeferredActions,
    ],
    name="triage",
)
