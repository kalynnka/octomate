from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    Literal,
    Protocol,
    TypeAlias,
    cast,
)

import logfire
from pydantic import JsonValue
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult, StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests
from pydantic_core import to_json
from typing_extensions import TypeVar

from octomate.capabilities.events import ResultSegmentEvent, ResultTextDeltaEvent
from octomate.schemas.conversation import ConversationKey

if TYPE_CHECKING:
    from octomate.tentacles.channel.base import Chromo, Ink

logger = logging.getLogger(__name__)

IMMessageID: TypeAlias = str
MessageT = TypeVar("MessageT")
RawT = TypeVar("RawT")
OutputT = TypeVar(
    "OutputT", bound=JsonValue | DeferredToolRequests, infer_variance=True
)
OutputContraT = TypeVar(
    "OutputContraT",
    bound=JsonValue | DeferredToolRequests,
    contravariant=True,
)


@dataclass(frozen=True)
class MarkdownChunker:
    DEFAULT_LIMIT: ClassVar[int] = 12_000

    limit: int = DEFAULT_LIMIT
    natural_min_size: int | None = None

    @property
    def effective_natural_min_size(self) -> int:
        if self.natural_min_size is not None:
            return self.natural_min_size
        return self.limit // 2

    def chunk(self, text: str) -> list[str]:
        if not text:
            return [""]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > self.limit:
            split_at = self.split_index(remaining)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    def split_index(self, text: str) -> int:
        for boundary in (
            self.last_separator_boundary(text, "\n\n"),
            self.last_separator_boundary(text, "\n"),
            self.last_sentence_boundary(text),
            self.last_whitespace_boundary(text),
        ):
            if boundary >= self.effective_natural_min_size:
                return boundary

        return self.last_whitespace_boundary(text) or self.limit

    def last_separator_boundary(self, text: str, separator: str) -> int:
        boundary = 0
        start = 0
        while True:
            index = text.find(separator, start, self.limit)
            if index < 0:
                return boundary
            candidate = index + len(separator)
            if candidate <= self.limit:
                boundary = candidate
            start = index + len(separator)

    def last_sentence_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r'[.!?][)"\']?\s+', text[: self.limit]):
            boundary = match.end()
        return boundary

    def last_whitespace_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r"\s+", text[: self.limit]):
            boundary = match.end()
        return boundary


StreamBlockType = Literal["answer", "thinking", "tool_call", "tool_result", "subagent"]
StreamBlockStatus = Literal["streaming", "done", "error"]


@dataclass(frozen=True)
class StreamBlock:
    id: str
    type: StreamBlockType = "answer"
    title: str = ""
    foldable: bool = False
    status: StreamBlockStatus = "streaming"


@dataclass(frozen=True)
class BatchedTextUpdate:
    block_id: str
    block_type: StreamBlockType
    title: str
    delta_text: str
    full_text: str
    sequence: int
    is_final: bool = False
    foldable: bool = False
    status: StreamBlockStatus = "streaming"


@dataclass(frozen=True)
class StreamEventDelta:
    block: StreamBlock
    text: str


@dataclass
class TextStreamBuffer:
    block: StreamBlock
    full_text: str = ""
    pending_delta: str = ""
    sequence: int = 0
    last_flush_at: float = 0.0


class TextStreamBatcher:
    def __init__(
        self,
        *,
        flush_interval: float = 0.5,
        min_chars: int = 120,
        max_chars: int = 1000,
        fold_threshold: int = 1500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.flush_interval = max(flush_interval, 0.0)
        self.min_chars = max(min_chars, 0)
        self.max_chars = max(max_chars, 1)
        self.fold_threshold = max(fold_threshold, 1)
        self.clock = clock
        self.buffers: dict[str, TextStreamBuffer] = {}
        self.active_block_id: str | None = None

    def push_text(
        self,
        text: str,
        *,
        block: StreamBlock | None = None,
    ) -> list[BatchedTextUpdate]:
        if not text:
            return []
        block = block or StreamBlock(id="answer")
        updates: list[BatchedTextUpdate] = []
        if self.active_block_id and self.active_block_id != block.id:
            previous = self.flush_block(self.active_block_id)
            if previous is not None:
                updates.append(previous)

        self.active_block_id = block.id
        buffer = self.buffers.get(block.id)
        if buffer is None:
            buffer = TextStreamBuffer(block=block, last_flush_at=self.clock())
            self.buffers[block.id] = buffer
        else:
            buffer.block = block

        buffer.full_text += text
        buffer.pending_delta += text
        if self.should_flush(buffer):
            update = self.flush_block(block.id)
            if update is not None:
                updates.append(update)
        return updates

    def flush_block(
        self,
        block_id: str,
        *,
        is_final: bool = False,
    ) -> BatchedTextUpdate | None:
        buffer = self.buffers.get(block_id)
        if buffer is None or not buffer.pending_delta:
            return None
        buffer.sequence += 1
        update = BatchedTextUpdate(
            block_id=buffer.block.id,
            block_type=buffer.block.type,
            title=buffer.block.title,
            delta_text=buffer.pending_delta,
            full_text=buffer.full_text,
            sequence=buffer.sequence,
            is_final=is_final,
            foldable=buffer.block.foldable
            or len(buffer.full_text) >= self.fold_threshold,
            status="done" if is_final else buffer.block.status,
        )
        buffer.pending_delta = ""
        buffer.last_flush_at = self.clock()
        return update

    def finish_all(self) -> list[BatchedTextUpdate]:
        updates: list[BatchedTextUpdate] = []
        if self.active_block_id:
            update = self.flush_block(self.active_block_id, is_final=True)
            if update is not None:
                updates.append(update)
        for block_id in list(self.buffers):
            if block_id == self.active_block_id:
                continue
            update = self.flush_block(block_id, is_final=True)
            if update is not None:
                updates.append(update)
        return updates

    def full_text(self, block_id: str = "answer") -> str:
        buffer = self.buffers.get(block_id)
        return buffer.full_text if buffer is not None else ""

    def should_flush(self, buffer: TextStreamBuffer) -> bool:
        if self.flush_interval <= 0:
            return True
        if len(buffer.pending_delta) >= self.max_chars:
            return True
        if len(buffer.pending_delta) < self.min_chars:
            return False
        return self.clock() - buffer.last_flush_at >= self.flush_interval


def render_stream_event_delta(
    event: AgentStreamEvent | AgentRunResultEvent[OutputT],
) -> StreamEventDelta | None:
    if isinstance(event, PartStartEvent):
        if isinstance(event.part, TextPart):
            return StreamEventDelta(
                block=StreamBlock(id=f"answer-{event.index}", type="answer"),
                text=event.part.content,
            )
        if isinstance(event.part, ThinkingPart):
            return StreamEventDelta(
                block=StreamBlock(
                    id=f"thinking-{event.index}",
                    type="thinking",
                    title="Thinking",
                    foldable=True,
                ),
                text=event.part.content,
            )
    if isinstance(event, PartDeltaEvent):
        if isinstance(event.delta, TextPartDelta):
            return StreamEventDelta(
                block=StreamBlock(id=f"answer-{event.index}", type="answer"),
                text=event.delta.content_delta,
            )
        if isinstance(event.delta, ThinkingPartDelta):
            return StreamEventDelta(
                block=StreamBlock(
                    id=f"thinking-{event.index}",
                    type="thinking",
                    title="Thinking",
                    foldable=True,
                ),
                text=event.delta.content_delta or "",
            )
    if isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
        tool = event.part
        title = f"Tool call: {tool.tool_name}"
        if isinstance(event, OutputToolCallEvent):
            title = f"Output tool: {tool.tool_name}"
        return StreamEventDelta(
            block=StreamBlock(
                id=f"tool-call-{tool.tool_call_id}",
                type="tool_call",
                title=title,
                foldable=True,
                status="done",
            ),
            text=format_stream_value(cast(JsonValue, tool.args_as_dict())),
        )
    if isinstance(event, FunctionToolResultEvent | OutputToolResultEvent):
        part = event.part
        tool_name = part.tool_name or "output"
        title = f"Tool result: {tool_name}"
        if isinstance(event, OutputToolResultEvent):
            title = f"Output result: {tool_name}"
        return StreamEventDelta(
            block=StreamBlock(
                id=f"tool-result-{part.tool_call_id}",
                type="tool_result",
                title=title,
                foldable=True,
                status="done",
            ),
            text=format_stream_value(tool_result_text(part)),
        )
    return None


def tool_result_text(part: ToolReturnPart | RetryPromptPart) -> str:
    if isinstance(part, ToolReturnPart):
        return part.model_response_str()
    return part.model_response()


def format_stream_value(value: JsonValue, *, max_chars: int = 2000) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = to_json(
            value,
            indent=2,
            ensure_ascii=False,
            fallback=str,
        ).decode()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n\n...[truncated]"


SKIPPED_PLAN_TOOL_NAMES = frozenset({"ask_questions"})
MAX_TASK_DETAIL_CHARS = 2000


def should_skip_plan_tool(tool_name: str) -> bool:
    return tool_name in SKIPPED_PLAN_TOOL_NAMES


def format_field_name(value: object) -> str:
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Value"


def humanize_tool_name(tool_name: str) -> str:
    name = tool_name.rsplit("__", 1)[-1].replace("-", " ")
    return format_field_name(name)


def status_hint(tool_name: str) -> str:
    return f"{humanize_tool_name(tool_name)}…"


def truncate_task_detail(text: str, *, max_chars: int = MAX_TASK_DETAIL_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 16].rstrip()}\n...[truncated]"


def format_inline_value(value: Any) -> str:
    if value is None:
        return "_None_"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not value:
            return "_None_"
        return ", ".join(format_inline_value(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(
            f"{format_field_name(key)}: {format_inline_value(item)}"
            for key, item in value.items()
        )
    return truncate_task_detail(str(value), max_chars=500)


def format_mapping_lines(
    value: dict[str, Any], *, indent: int = 0, bold: str = "*"
) -> list[str]:
    lines: list[str] = []
    pad = "   " * indent
    for key, item in value.items():
        label = format_field_name(key)
        if isinstance(item, dict):
            lines.append(f"{pad}{bold}{label}:{bold}")
            lines.extend(format_mapping_lines(item, indent=indent + 1, bold=bold))
        elif (
            isinstance(item, list)
            and item
            and any(isinstance(entry, (dict, list)) for entry in item)
        ):
            lines.append(f"{pad}{bold}{label}:{bold}")
            lines.extend(format_list_lines(item, indent=indent + 1, bold=bold))
        else:
            lines.append(f"{pad}{bold}{label}:{bold} {format_inline_value(item)}")
    return lines


def format_list_lines(
    value: list[Any], *, indent: int = 0, bold: str = "*"
) -> list[str]:
    lines: list[str] = []
    pad = "   " * indent
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            lines.append(f"{pad}{index}.")
            lines.extend(format_mapping_lines(item, indent=indent + 1, bold=bold))
        else:
            lines.append(f"{pad}{index}. {format_inline_value(item)}")
    return lines


def format_fields(value: dict[str, Any], *, bold: str = "*") -> str:
    lines = format_mapping_lines(value, bold=bold)
    return truncate_task_detail("\n".join(lines) if lines else "_No details_")


class MarkdownFeeler(Protocol):
    """Presents markdown to IM and returns platform message metadata only.

    Return ``IMMessageID`` values for IM bookkeeping, such as a message id,
    timestamp, or card id. Feelers must not return agent/runtime results.
    """

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> IMMessageID | None: ...


class MarkdownStreamFeeler(Protocol[OutputT]):
    """Presents streamed markdown to IM and returns platform message metadata only."""

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, OutputT],
    ) -> IMMessageID | None: ...

    async def present_output(
        self,
        key: ConversationKey,
        events: AsyncIterator[
            ResultTextDeltaEvent | ResultSegmentEvent | FinalResult[OutputT]
        ],
    ) -> IMMessageID | None: ...


class EventStreamFeeler(Protocol[OutputContraT]):
    """Presents agent events to IM and returns platform message metadata only."""

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputContraT]],
    ) -> IMMessageID | None: ...


async def final_stream_result(
    events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
) -> AgentRunResult[OutputT] | None:
    result: AgentRunResult[OutputT] | None = None
    async for event in events:
        if isinstance(event, AgentRunResultEvent):
            result = event.result
    return result


def markdown_from_output(output: JsonValue | DeferredToolRequests) -> str | None:
    if output is None or isinstance(output, DeferredToolRequests):
        return None
    if isinstance(output, str):
        return output
    return f"```json\n{format_stream_value(output)}\n```"


async def present_markdown(
    *,
    ink: Ink[MessageT],
    chromo: Chromo[RawT, MessageT],
    key: ConversationKey,
    markdown: str,
) -> IMMessageID | None:
    chat_id = key.chat_id or key.user_id
    chat_type = key.chat_type
    reply_to: str | None = key.thread_id or None
    first_message_id: IMMessageID | None = None
    with logfire.span(
        "present_markdown",
        channel_id=key.channel_tentacle_id,
        chat_type=chat_type,
        markdown_len=len(markdown),
    ) as span:
        for message in chromo.outbound_markdown(markdown):
            message_id = await ink.send_message(
                chat_id,
                chat_type,
                [message],
                reply_to,
            )
            first_message_id = first_message_id or message_id
        span.set_attribute("message_id", str(first_message_id))
        return first_message_id


class DefaultMarkdownFeeler(Generic[RawT, MessageT]):
    def __init__(self, *, ink: Ink[MessageT], chromo: Chromo[RawT, MessageT]) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> IMMessageID | None:
        return await present_markdown(
            ink=self.ink,
            chromo=self.chromo,
            key=key,
            markdown=markdown,
        )


class DefaultMarkdownStreamFeeler(Generic[RawT, MessageT, OutputT]):
    def __init__(self, *, ink: Ink[MessageT], chromo: Chromo[RawT, MessageT]) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, OutputT],
    ) -> IMMessageID | None:
        return await self.present_final(
            key,
            stream.stream_output(debounce_by=None),
            final_output=stream.get_output,
        )

    async def present_output(
        self,
        key: ConversationKey,
        events: AsyncIterator[
            ResultTextDeltaEvent | ResultSegmentEvent | FinalResult[OutputT]
        ],
    ) -> IMMessageID | None:
        # No streaming Ink (NapCat): consume the stream and send one final message.
        final_output: OutputT | None = None
        parts: list[str] = []
        async for event in events:
            if isinstance(event, FinalResult):
                final_output = event.output
            elif isinstance(event, ResultTextDeltaEvent):
                parts.append(event.delta)
            else:
                parts.append(str(event.segment))
        markdown = (
            markdown_from_output(final_output) if final_output is not None else None
        )
        if markdown is None and parts:
            markdown = "".join(parts)
        if markdown is not None:
            logger.warning(
                "Channel %s: stream feeler has no streaming transport; "
                "sending the reply as a single message",
                key.channel_tentacle_id,
            )
            return await present_markdown(
                ink=self.ink,
                chromo=self.chromo,
                key=key,
                markdown=markdown,
            )
        return None

    async def present_final(
        self,
        key: ConversationKey,
        snapshots: AsyncIterator[OutputT],
        *,
        final_output: Callable[[], Awaitable[OutputT | None]] | None,
    ) -> IMMessageID | None:
        last: OutputT | None = None
        async for snapshot in snapshots:
            last = snapshot
        if last is None and final_output is not None:
            last = await final_output()
        markdown = markdown_from_output(last) if last is not None else None
        if markdown is not None:
            return await present_markdown(
                ink=self.ink,
                chromo=self.chromo,
                key=key,
                markdown=markdown,
            )
        return None


class DefaultEventStreamFeeler(Generic[RawT, MessageT, OutputT]):
    def __init__(self, *, ink: Ink[MessageT], chromo: Chromo[RawT, MessageT]) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
    ) -> IMMessageID | None:
        result = await final_stream_result(events)
        markdown = markdown_from_output(result.output) if result is not None else None
        if markdown is not None:
            return await present_markdown(
                ink=self.ink,
                chromo=self.chromo,
                key=key,
                markdown=markdown,
            )
        return None
