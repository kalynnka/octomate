from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import ChannelTentacle

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
For action="reception", keep answer empty and explain the routing in reason/title.
"""


class TriageDecision(BaseModel):
    action: TriageAction
    answer: str = ""
    target_id: str = ""
    reason: str = ""
    title: str = ""


@dataclass(frozen=True)
class ResponseTarget:
    id: str
    channel_id: str
    key: ConversationKey
    mode: ResponseTargetMode = "main"

    def __str__(self) -> str:
        return (
            f"- {self.id}: channel={self.channel_id}, "
            f"chat_type={self.key.chat_type}, mode={self.mode}"
        )


@dataclass
class TriageGraphResult:
    decision: TriageDecision
    target: ResponseTarget
    result: AgentRunResult[Any] | None = None


@dataclass
class TriageState:
    message_history: list[ModelMessage] = field(default_factory=list)


@dataclass
class TriageDeps:
    agent: AgentTentacle
    conversation_key: ConversationKey
    targets: dict[str, ResponseTarget]
    channels: dict[str, ChannelTentacle]
    source_events: list[MessageEvent]
    direct_target_id: str
    reception_target_id: str


@dataclass
class RunTriage(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    user_prompt: str | Sequence[UserContent] | None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> DispatchTriage:
        result = await ctx.deps.agent.run(
            self.user_prompt,
            conversation_key=ctx.deps.conversation_key,
            run_name="triage",
            output_type=TriageDecision,
            message_history=ctx.state.message_history,
            instructions=TRIAGE_INSTRUCTIONS.format(
                targets="\n".join(str(target) for target in ctx.deps.targets.values())
            ),
        )
        ctx.state.message_history = list(result.all_messages())

        if not isinstance(result.output, TriageDecision):
            raise RuntimeError(
                f"triage graph expected TriageDecision, got {type(result.output)!r}"
            )

        decision = result.output
        fallback_target_id = (
            ctx.deps.direct_target_id
            if decision.action == "answer"
            else ctx.deps.reception_target_id
        )
        target = ctx.deps.targets.get(decision.target_id) or ctx.deps.targets[
            fallback_target_id
        ]
        if decision.action == "answer" and target.mode != "main":
            target = replace(target, mode="main")
        elif decision.action == "reception" and target.mode != "sub":
            target = replace(target, mode="sub")

        return DispatchTriage(
            user_prompt=self.user_prompt,
            decision=decision,
            target=target,
        )


@dataclass
class DispatchTriage(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    user_prompt: str | Sequence[UserContent] | None
    decision: TriageDecision
    target: ResponseTarget

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> RunReception | End[TriageGraphResult]:
        target_channel = ctx.deps.channels.get(self.target.channel_id)
        if target_channel is None:
            raise ValueError(f"unknown channel {self.target.channel_id!r}")

        source_events = (
            ctx.deps.source_events
            if self.target.channel_id == ctx.deps.conversation_key.channel_tentacle_id
            else None
        )
        if self.decision.action == "answer":
            await target_channel.respond_text(
                self.target.key,
                self.decision.answer or self.decision.reason,
                source_events=source_events,
            )
            return End(TriageGraphResult(decision=self.decision, target=self.target))

        target_key = self.target.key
        if self.target.channel_id != ctx.deps.conversation_key.channel_tentacle_id:
            target_key = await target_channel.start_sub_thread(
                self.target.key,
                self.decision.title
                or self.decision.reason
                or "Octomate is continuing this request here.",
            )
            source_events = None

        return RunReception(
            user_prompt=self.user_prompt,
            decision=self.decision,
            target=replace(self.target, key=target_key),
            target_key=target_key,
            source_events=source_events,
        )


@dataclass
class RunReception(
    BaseNode[TriageState, TriageDeps, TriageGraphResult],
):
    user_prompt: str | Sequence[UserContent] | None
    decision: TriageDecision
    target: ResponseTarget
    target_key: ConversationKey
    source_events: list[MessageEvent] | None
    result: AgentRunResult[Any] | None = None

    async def run(
        self,
        ctx: GraphRunContext[TriageState, TriageDeps],
    ) -> End[TriageGraphResult]:
        target_channel = ctx.deps.channels.get(self.target.channel_id)
        if target_channel is None:
            raise ValueError(f"unknown channel {self.target.channel_id!r}")

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            async with ctx.deps.agent.run_stream_events(
                self.user_prompt,
                conversation_key=ctx.deps.conversation_key,
                run_name="reception",
                message_history=ctx.state.message_history,
            ) as stream:
                async for event in stream:
                    if isinstance(event, AgentRunResultEvent):
                        self.result = event.result
                    yield event

        if target_channel.config.stream.enabled:
            await target_channel.stream_respond(
                self.target_key,
                events(),
                source_events=self.source_events,
            )
        else:
            await target_channel.respond(
                self.target_key,
                events(),
                source_events=self.source_events,
            )

        if self.result is not None:
            ctx.state.message_history = list(self.result.all_messages())
        return End(
            TriageGraphResult(
                decision=self.decision,
                target=self.target,
                result=self.result,
            )
        )


triage_graph = Graph[TriageState, TriageDeps, TriageGraphResult](
    nodes=[RunTriage, DispatchTriage, RunReception],
    name="triage",
)
