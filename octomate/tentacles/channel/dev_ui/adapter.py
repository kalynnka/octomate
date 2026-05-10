from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi.responses import StreamingResponse
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.tools import (
    DeferredToolApprovalResult,
    DeferredToolRequests,
    DeferredToolResults,
    ToolDenied,
)
from pydantic_ai.ui.vercel_ai._event_stream import VERCEL_AI_DSP_HEADERS
from pydantic_ai.ui.vercel_ai._utils import iter_tool_approval_responses
from pydantic_ai.ui.vercel_ai.request_types import (
    RequestData,
    SubmitMessage,
    TextUIPart,
    UIMessage,
)
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    DoneChunk,
    FinishChunk,
    FinishStepChunk,
    ReasoningStartChunk,
    StartChunk,
    StartStepChunk,
    TextDeltaChunk,
    TextEndChunk,
    TextStartChunk,
    ToolApprovalRequestChunk,
    ToolInputAvailableChunk,
    ToolInputDeltaChunk,
    ToolInputStartChunk,
)

from octomate.managers.conversations import ConversationManager
from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agent.inkling.graph import (
    InklingDeps,
    InklingOutput,
    InklingState,
    ResolveDeferred,
    ResumeTurn,
    StartTurn,
    inkling_graph,
)
from octomate.tentacles.agent.inkling.resolver import DeferredResolver

logger = logging.getLogger(__name__)


class NeverResolver(DeferredResolver):
    """Defensive resolver — DevUI intercepts ResolveDeferred at the graph-iter boundary."""

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        raise AssertionError(
            "DevUI's GraphAdapter should intercept ResolveDeferred before this is called"
        )


@dataclass
class DeferredSentinel:
    requests: DeferredToolRequests


@dataclass
class FinalSentinel:
    output: list[AgentMessage]


class StreamEnd: ...


STREAM_END = StreamEnd()


@dataclass
class GraphAdapter:
    """Translates Vercel AI Data Stream Protocol requests into inkling_graph runs.

    Each `/api/chat` POST is a stateless slice: load history from the
    conversation store, run one graph invocation, persist new messages,
    stream events. Round-2 approvals are pulled from the request body and
    fed back as `deferred_tool_results`.
    """

    SDK_VERSION: ClassVar[int] = 6

    tentacle_id: str
    agent: Agent[None, InklingOutput]
    conversations: ConversationManager
    agent_id: str = "Inkling"

    async def handle_request(self, body: RequestData) -> StreamingResponse:
        if not isinstance(body, SubmitMessage):
            return StreamingResponse(
                self._noop_stream(),
                media_type="text/event-stream",
                headers=VERCEL_AI_DSP_HEADERS,
            )

        chat_id = body.id
        user_text = self._extract_latest_user_text(body.messages)
        deferred = self._extract_deferred_results(body.messages)

        conversation = await self.conversations.ensure(
            ConversationKey(
                channel_tentacle_id=self.tentacle_id,
                chat_type="private",
                chat_id=chat_id,
                user_id="dev",
                thread_id="",
            ),
            agent_tentacle_id=self.agent_id,
        )
        # Conversation.messages is a viewonly secondary relationship across
        # all AgentRuns, loaded eagerly via selectin — already in
        # conversation order.
        history: list[ModelMessage] = list(conversation.messages)

        return StreamingResponse(
            self._stream(
                conversation=conversation,
                history=history,
                user_text=user_text,
                deferred=deferred,
            ),
            media_type="text/event-stream",
            headers=VERCEL_AI_DSP_HEADERS,
        )

    async def _stream(
        self,
        *,
        conversation: Conversation,
        history: list[ModelMessage],
        user_text: str,
        deferred: DeferredToolResults | None,
    ) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[
            AgentStreamEvent | DeferredSentinel | FinalSentinel | StreamEnd
        ] = asyncio.Queue()

        async def sink(event: AgentStreamEvent) -> None:
            await queue.put(event)

        deps = InklingDeps(
            agent=self.agent,
            resolver=NeverResolver(),
            event_sink=sink,
            conversation_manager=self.conversations,
        )
        state = InklingState(
            message_history=list(history),
            conversation=conversation,
        )
        start_node = (
            ResumeTurn(deferred_results=deferred)
            if deferred is not None
            else StartTurn(user_prompt=user_text)
        )

        async def driver() -> None:
            try:
                async with inkling_graph.iter(
                    start_node,
                    state=state,
                    deps=deps,
                ) as run:
                    async for node in run:
                        if isinstance(node, ResolveDeferred):
                            await queue.put(DeferredSentinel(node.requests))
                            return
                    if run.result is not None:
                        await queue.put(FinalSentinel(run.result.output))
            except Exception:
                logger.exception("DevUI graph driver failed")
                await queue.put(FinalSentinel([]))
            finally:
                await queue.put(STREAM_END)

        task = asyncio.create_task(driver())

        yield self._encode(StartChunk(message_id=str(uuid.uuid4())))
        yield self._encode(StartStepChunk())

        try:
            while True:
                item = await queue.get()
                if isinstance(item, StreamEnd):
                    break
                if isinstance(item, DeferredSentinel):
                    for chunk in self._emit_approval_requests(item.requests):
                        yield self._encode(chunk)
                    continue
                if isinstance(item, FinalSentinel):
                    for chunk in self._emit_final_messages(item.output):
                        yield self._encode(chunk)
                    continue
                for chunk in self._translate(item):
                    yield self._encode(chunk)
        finally:
            yield self._encode(FinishStepChunk())
            yield self._encode(FinishChunk())
            yield self._encode(DoneChunk())
            await task

    async def _noop_stream(self) -> AsyncIterator[bytes]:
        yield self._encode(StartChunk())
        yield self._encode(DoneChunk())

    @classmethod
    def _encode(cls, chunk: BaseChunk) -> bytes:
        return f"data: {chunk.encode(cls.SDK_VERSION)}\n\n".encode()

    @staticmethod
    def _extract_latest_user_text(messages: list[UIMessage]) -> str:
        for msg in reversed(messages):
            if msg.role != "user":
                continue
            text_parts = [p.text for p in msg.parts if isinstance(p, TextUIPart)]
            if text_parts:
                return "\n".join(text_parts)
        return ""

    @staticmethod
    def _extract_deferred_results(
        messages: list[UIMessage],
    ) -> DeferredToolResults | None:
        approvals: dict[str, DeferredToolApprovalResult | bool] = {}
        for tool_call_id, approval in iter_tool_approval_responses(messages):
            if approval.approved:
                approvals[tool_call_id] = True
            elif approval.reason:
                approvals[tool_call_id] = ToolDenied(message=approval.reason)
            else:
                approvals[tool_call_id] = False
        if not approvals:
            return None
        return DeferredToolResults(approvals=approvals)

    def _translate(self, event: AgentStreamEvent) -> list[BaseChunk]:
        if isinstance(event, PartStartEvent):
            return self._translate_part_start(event.part, event.index)
        if isinstance(event, PartDeltaEvent):
            return self._translate_part_delta(event.delta, event.index)
        if isinstance(event, PartEndEvent):
            return self._translate_part_end(event.part, event.index)
        return []

    def _translate_part_start(self, part: Any, index: int) -> list[BaseChunk]:
        if isinstance(part, TextPart):
            chunks: list[BaseChunk] = [TextStartChunk(id=f"t-{index}")]
            if part.content:
                chunks.append(TextDeltaChunk(id=f"t-{index}", delta=part.content))
            return chunks
        if isinstance(part, ThinkingPart):
            return [ReasoningStartChunk(id=f"r-{index}")]
        if isinstance(part, ToolCallPart):
            chunks = [
                ToolInputStartChunk(
                    tool_call_id=part.tool_call_id,
                    tool_name=part.tool_name,
                ),
            ]
            args_str = self._args_to_str(part.args)
            if args_str:
                chunks.append(
                    ToolInputDeltaChunk(
                        tool_call_id=part.tool_call_id,
                        input_text_delta=args_str,
                    )
                )
            return chunks
        return []

    def _translate_part_delta(self, delta: Any, index: int) -> list[BaseChunk]:
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            return [TextDeltaChunk(id=f"t-{index}", delta=delta.content_delta)]
        if isinstance(delta, ToolCallPartDelta):
            tool_call_id = delta.tool_call_id or ""
            args_str = self._args_to_str(delta.args_delta)
            if not args_str:
                return []
            return [
                ToolInputDeltaChunk(
                    tool_call_id=tool_call_id,
                    input_text_delta=args_str,
                )
            ]
        return []

    def _translate_part_end(self, part: Any, index: int) -> list[BaseChunk]:
        if isinstance(part, TextPart):
            return [TextEndChunk(id=f"t-{index}")]
        if isinstance(part, ToolCallPart):
            args = (
                part.args_as_dict()
                if callable(getattr(part, "args_as_dict", None))
                else {}
            )
            return [
                ToolInputAvailableChunk(
                    tool_call_id=part.tool_call_id,
                    tool_name=part.tool_name,
                    input=args,
                )
            ]
        return []

    def _emit_final_messages(self, output: list[AgentMessage]) -> list[BaseChunk]:
        chunks: list[BaseChunk] = []
        for i, msg in enumerate(output):
            text_id = f"final-{i}"
            chunks.append(TextStartChunk(id=text_id))
            for seg in msg.segments:
                text = self._segment_text(seg)
                if text:
                    chunks.append(TextDeltaChunk(id=text_id, delta=text))
            chunks.append(TextEndChunk(id=text_id))
        return chunks

    def _emit_approval_requests(
        self, requests: DeferredToolRequests
    ) -> list[BaseChunk]:
        return [
            ToolApprovalRequestChunk(
                approval_id=str(uuid.uuid4()),
                tool_call_id=call.tool_call_id,
            )
            for call in requests.approvals
        ]

    @staticmethod
    def _args_to_str(args: Any) -> str:
        if args is None or args == "":
            return ""
        if isinstance(args, str):
            return args
        return json.dumps(args)

    @staticmethod
    def _segment_text(seg: MessageSegment) -> str:
        data = getattr(seg, "data", None)
        if isinstance(data, dict) and "text" in data:
            return str(data["text"])
        return str(seg)
