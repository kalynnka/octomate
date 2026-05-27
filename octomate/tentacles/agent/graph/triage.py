from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy

logger = logging.getLogger(__name__)

TriageAction = Literal["answer", "reception"]
ResponseTargetMode = Literal["main", "sub"]

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


class TriageDecision(BaseModel):
    action: TriageAction
    answer: str = ""
    target_id: str = ""
    reason: str = ""
    hint: str = ""
    handoff: str = ""


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
class TriageGraphResult:
    decision: TriageDecision
    target: ResponseTarget
    result: AgentRunResult[Any] | None = None


@dataclass
class TriageState:
    pass


@dataclass
class TriageDeps:
    agent: AgentTentacle

    source_target: ResponseTarget

    channels: dict[str, ChannelTentacle]
    conversation_manager: ConversationManager

    @cached_property
    def source_key(self) -> ConversationKey:
        if self.source_target.key is None:
            raise ValueError("TriageDeps.source_target requires a ConversationKey")
        return self.source_target.key

    def channel_for(self, target: ResponseTarget) -> ChannelTentacle:
        channel = self.channels.get(target.channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {target.channel_id!r}")
        return channel

    @cached_property
    def response_targets(self) -> dict[str, ResponseTarget]:
        source_key = self.source_key
        targets = {
            channel_id: ResponseTarget(
                channel_id=channel_id,
                key=source_key if channel_id == self.source_target.channel_id else None,
                thread_strategy=channel.thread_strategy,
                mode="main",
            )
            for channel_id, channel in self.channels.items()
        }
        targets[self.source_target.channel_id] = self.source_target
        return targets


@dataclass
class RunTriage(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    user_prompt: str | Sequence[UserContent] | None
    message_history: list[ModelMessage] = field(default_factory=list)

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> AnswerDirect | PrepareReception | RunReception:
        source_target = ctx.deps.source_target
        source_key = ctx.deps.source_key
        targets = ctx.deps.response_targets
        if source_key.thread_id and source_target.thread_strategy == "flat_thread":
            return RunReception(
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

        result = await ctx.deps.agent.run(
            self.user_prompt,
            conversation_key=source_key,
            run_name="triage",
            output_type=TriageDecision,
            message_history=self.message_history,
            instructions=TRIAGE_INSTRUCTIONS.format(
                targets="\n".join(str(target) for target in targets.values())
            ),
        )

        decision = result.output
        target = targets.get(decision.target_id) or source_target
        if decision.action == "answer" and target.mode != "main":
            target = replace(target, mode="main")
        elif decision.action == "reception" and target.mode != "sub":
            target = replace(target, mode="sub")

        if decision.action == "answer":
            return AnswerDirect(decision=decision, target=target)
        return PrepareReception(
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
        await target_channel.respond_text(
            self.target.key,
            self.decision.answer or self.decision.reason,
        )
        return End(TriageGraphResult(decision=self.decision, target=self.target))


@dataclass
class PrepareReception(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
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
            source_key = ctx.deps.source_key
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
            user_prompt=self.decision.handoff or str(self.user_prompt or ""),
            decision=self.decision,
            target=target,
            target_key=target_key,
            message_history=(
                list(self.source_message_history)
                if target_key == ctx.deps.source_key and target.mode == "main"
                else None
            ),
        )


@dataclass
class RunReception(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    user_prompt: str | Sequence[UserContent] | None
    decision: TriageDecision
    target: ResponseTarget
    target_key: ConversationKey
    message_history: list[ModelMessage] | None = None
    result: AgentRunResult[Any] | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> End[TriageGraphResult]:
        target_channel = ctx.deps.channel_for(self.target)
        if self.message_history is not None:
            message_history = self.message_history
        else:
            conversation = await ctx.deps.conversation_manager.ensure(
                self.target_key,
                agent_tentacle_id=ctx.deps.agent.id,
            )
            message_history = list(conversation.messages)

        if target_channel.config.stream.enabled:

            async def stream_events() -> AsyncIterator[
                AgentStreamEvent | AgentRunResultEvent[Any]
            ]:
                async with ctx.deps.agent.run_stream_events(
                    self.user_prompt,
                    conversation_key=self.target_key,
                    run_name="reception",
                    message_history=message_history,
                ) as stream:
                    async for event in stream:
                        if isinstance(event, AgentRunResultEvent):
                            self.result = event.result
                        yield event

            await target_channel.stream_respond(
                self.target_key,
                stream_events(),
            )
        else:
            self.result = await ctx.deps.agent.run(
                self.user_prompt,
                conversation_key=self.target_key,
                run_name="reception",
                message_history=message_history,
            )

            async def result_events() -> AsyncIterator[AgentRunResultEvent[Any]]:
                if self.result is not None:
                    yield AgentRunResultEvent(self.result)

            await target_channel.respond(
                self.target_key,
                result_events(),
            )

        return End(
            TriageGraphResult(
                decision=self.decision,
                target=self.target,
                result=self.result,
            )
        )


triage_graph = Graph[TriageState, TriageDeps, TriageGraphResult](
    nodes=[RunTriage, AnswerDirect, PrepareReception, RunReception],
    name="triage",
)
