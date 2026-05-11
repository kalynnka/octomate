"""DevUI translation between octopus signals and Vercel AI chunks.

Two pieces:

- `extract_user_text` / `extract_deferred_results` parse the inbound
  Vercel `SubmitMessage` body into octopus inputs (a user text prompt
  and optional deferred-tool approval results).
- `DevUIStreamSink` consumes the agent's `StreamFrame`s for one request,
  encodes them into Vercel chunks, and feeds an SSE response generator
  via an internal queue. It also handles the discrete `SendSegments`
  branch (final messages, approval cards) via `write_segments`.

A new sink is constructed per `POST /api/chat` request and pre-registered
in `octopus.sinks` before kicking. When the agent emits `StreamFrame(close)`,
the sink's `aclose()` runs, the queue yields its terminal Vercel chunks,
and the SSE generator returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic_ai.messages import (
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
from pydantic_ai.ui.vercel_ai._utils import iter_tool_approval_responses
from pydantic_ai.ui.vercel_ai.request_types import TextUIPart, UIMessage
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

from octomate.nerve import SendSegments, StreamFrame
from octomate.schemas.segments import MessageSegment

logger = logging.getLogger(__name__)

SDK_VERSION: ClassVar = 6  # type: ignore[valid-type]


def extract_user_text(messages: list[UIMessage]) -> str:
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        text_parts = [p.text for p in msg.parts if isinstance(p, TextUIPart)]
        if text_parts:
            return "\n".join(text_parts)
    return ""


def extract_deferred_results(messages: list[UIMessage]) -> DeferredToolResults | None:
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


class DevUIStreamSink:
    """Per-request streaming sink. Implements `nerve.StreamSink`."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = False
        self._closed = False

    async def write(self, frame: StreamFrame) -> None:
        """Receive a `StreamFrame` from the agent and encode it to Vercel chunks."""
        if frame.frame_type == "close":
            await self.aclose()
            return
        payload = frame.payload or {}
        if (event := payload.get("event")) is not None:
            for chunk in self._translate_agent_event(event):
                await self._enqueue(chunk)
        if (deferred := payload.get("deferred")) is not None:
            for chunk in self._emit_approval_requests(deferred):
                await self._enqueue(chunk)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for chunk in (FinishStepChunk(), FinishChunk(), DoneChunk()):
            await self._enqueue(chunk)
        # Sentinel — tells the SSE generator to stop.
        await self._queue.put(None)

    async def write_segments(self, sig: SendSegments) -> None:
        """Handle a discrete `SendSegments` (final reply) — encode as text chunks."""
        text_id = f"final-{uuid.uuid4().hex[:8]}"
        await self._enqueue(TextStartChunk(id=text_id))
        for seg in sig.segments:
            text = _segment_text(seg)
            if text:
                await self._enqueue(TextDeltaChunk(id=text_id, delta=text))
        await self._enqueue(TextEndChunk(id=text_id))

    async def iter_sse(self) -> AsyncIterator[bytes]:
        """SSE generator — yields `data:` framed Vercel chunks until close."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _enqueue(self, chunk: BaseChunk) -> None:
        if not self._started:
            self._started = True
            await self._queue.put(_encode(StartChunk(message_id=str(uuid.uuid4()))))
            await self._queue.put(_encode(StartStepChunk()))
        await self._queue.put(_encode(chunk))

    @staticmethod
    def _emit_approval_requests(requests: DeferredToolRequests) -> list[BaseChunk]:
        return [
            ToolApprovalRequestChunk(
                approval_id=str(uuid.uuid4()),
                tool_call_id=call.tool_call_id,
            )
            for call in requests.approvals
        ]

    @classmethod
    def _translate_agent_event(cls, event: Any) -> list[BaseChunk]:
        if isinstance(event, PartStartEvent):
            return cls._translate_part_start(event.part, event.index)
        if isinstance(event, PartDeltaEvent):
            return cls._translate_part_delta(event.delta, event.index)
        if isinstance(event, PartEndEvent):
            return cls._translate_part_end(event.part, event.index)
        return []

    @classmethod
    def _translate_part_start(cls, part: Any, index: int) -> list[BaseChunk]:
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
            args_str = _args_to_str(part.args)
            if args_str:
                chunks.append(
                    ToolInputDeltaChunk(
                        tool_call_id=part.tool_call_id,
                        input_text_delta=args_str,
                    )
                )
            return chunks
        return []

    @classmethod
    def _translate_part_delta(cls, delta: Any, index: int) -> list[BaseChunk]:
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            return [TextDeltaChunk(id=f"t-{index}", delta=delta.content_delta)]
        if isinstance(delta, ToolCallPartDelta):
            tool_call_id = delta.tool_call_id or ""
            args_str = _args_to_str(delta.args_delta)
            if not args_str:
                return []
            return [
                ToolInputDeltaChunk(
                    tool_call_id=tool_call_id,
                    input_text_delta=args_str,
                )
            ]
        return []

    @classmethod
    def _translate_part_end(cls, part: Any, index: int) -> list[BaseChunk]:
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


def _encode(chunk: BaseChunk) -> bytes:
    return f"data: {chunk.encode(SDK_VERSION)}\n\n".encode()


def _args_to_str(args: Any) -> str:
    if args is None or args == "":
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args)


def _segment_text(seg: MessageSegment) -> str:
    data = getattr(seg, "data", None)
    if isinstance(data, dict) and "text" in data:
        return str(data["text"])
    return str(seg)
