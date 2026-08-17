from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Literal,
    Protocol,
    TypeAlias,
    cast,
)

from pydantic import JsonValue
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent

# pydantic-ai's default output-tool name lives in a private module (no public
# re-export), like the build_run_context import the agent harness already uses.
from pydantic_ai._output import DEFAULT_OUTPUT_TOOL_NAME
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests
from pydantic_core import to_json
from typing_extensions import TypeVar

from octomate.capabilities.ask import ASK_QUESTIONS_TOOL_NAME
from octomate.capabilities.gateway import (
    COMMISSION_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    WHISPER_TOOL_NAME,
)
from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    MessageSentEvent,
    OAuthAuthorizationEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    StreamEvents,
    SubagentActivity,
    SubagentActivityStatus,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.messages import SEND_TOOL_NAME
from octomate.schemas.segments import MessageSegment, ReplySegment, Segment
from octomate.schemas.todos import Todo
from octomate.telemetry import channel_logfire
from octomate.types.todos import STATUS_MARKERS

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager
    from octomate.tentacles.channels.base import ChannelOutput, Chromo, Ink
    from octomate.tentacles.channels.feelers.deferred import (
        ApprovalFeeler,
        QuestionFeeler,
    )
    from octomate.tentacles.channels.feelers.oauth import OAuthFeeler

logger = logging.getLogger(__name__)

IMMessageID: TypeAlias = str
MessageT = TypeVar("MessageT")
RawT = TypeVar("RawT")
OutputT = TypeVar(
    "OutputT",
    bound=JsonValue | Sequence[MessageSegment] | DeferredToolRequests,
    infer_variance=True,
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

    def reset(self) -> None:
        """Drop accumulated buffers so the same batcher can back the next message
        without carrying over `full_text` (which drives the fold threshold). The
        configured pacing (flush_interval/min_chars/...) is preserved."""
        self.buffers.clear()
        self.active_block_id = None

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


class StreamFlusher:
    """Single-flight background flusher for streaming renders (thinking/answer,
    on every channel). `signal()` wakes a lone background task that runs the
    `flush` callback — which sends whatever has accumulated; deltas that arrive
    while a flush is in flight coalesce into the next pass, so the drive loop
    never blocks on a slow platform round-trip. `drain()` stops the task; the
    caller sends any final/settled state itself afterwards."""

    def __init__(self, flush: Callable[[], Awaitable[None]]) -> None:
        self.flush = flush
        self.task: asyncio.Task[None] | None = None
        self.event = asyncio.Event()
        self.closed = False

    def signal(self) -> None:
        if self.task is None or self.task.done():
            self.closed = False
            self.event.clear()
            self.task = asyncio.create_task(self.run())
        self.event.set()

    async def run(self) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            if self.closed:
                return
            await self.flush()

    async def drain(self) -> None:
        self.closed = True
        self.event.set()
        if self.task is not None:
            await self.task
            self.task = None


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


# Tools the timeline draws nothing for: ask_questions and final_result render
# through their own surfaces, and `send` renders itself — its
# `MessageSentEvent` lands as a mid-run message in the timeline. Each name is
# sourced from its defining module so the literal lives in exactly one place.
SKIPPED_PLAN_TOOL_NAMES = frozenset(
    {
        ASK_QUESTIONS_TOOL_NAME,
        DEFAULT_OUTPUT_TOOL_NAME,
        SEND_TOOL_NAME,
        # Subagent calls render as separate timelines instead of parent plan rows.
        COMMISSION_TOOL_NAME,
        WHISPER_TOOL_NAME,
        TELEPORT_TOOL_NAME,  # Teleport is internal routing plumbing (relocate the conversation)
    }
)
MAX_TASK_DETAIL_CHARS = 2000


def should_skip_plan_tool(tool_name: str) -> bool:
    return tool_name in SKIPPED_PLAN_TOOL_NAMES


def format_field_name(value: object) -> str:
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Value"


def humanize_tool_name(tool_name: str) -> str:
    name = tool_name.rsplit("__", 1)[-1].replace("-", " ")
    return format_field_name(name)


def truncate_task_detail(text: str, *, max_chars: int = MAX_TASK_DETAIL_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 16].rstrip()}\n...[truncated]"


def format_inline_value(value: JsonValue) -> str:
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
        address: ChannelAddress,
        markdown: str,
    ) -> IMMessageID | None: ...


class SubagentTimelineState:
    """One independently rendered commissioned child run."""

    async def append_response(self, delta: str) -> None: ...

    async def settle(
        self,
        status: SubagentActivityStatus,
        detail: str | None = None,
    ) -> None: ...


@dataclass
class OpenSubagentTimeline:
    context: AbstractAsyncContextManager[SubagentTimelineState]
    state: SubagentTimelineState


class TimelineState:
    """Streaming render hook for one run, and the event→render dispatch (`drive`)
    that walks the run stream. The feeler that opened the state calls `bind` with
    the per-run context (address + the deferred-action feelers), then hands the stream
    to `drive`, which maps one lifecycle method per real stream event:
    `PartStart`/`PartDelta`/`PartEnd` of the thinking and answer parts map to
    `*_start` / `*_delta` / `*_end`, and the tool call/result events to
    `tool_start` / `tool_end`. A per-run timeline subclasses this and overrides
    only the lifecycle points it actually renders; the base methods render
    nothing. `message_id` carries the platform id of the rendered reply once
    the run's context closes.

    Mid-run notice contract: answer hooks that render reply text set `noticed`,
    and every hook that opens a NEW timeline entry (a thinking block, a tool
    call, a todo update — never a tool result, so in-flight work finishes where
    it began) calls `begin_entry()` first, which `rotate()`s once when a notice
    streamed in between. Overrides must keep doing both."""

    message_id: IMMessageID | None = None
    noticed: bool = False
    # The first `ReplySegment` seen sets this to the message id the reply threads
    # onto; send sites prefer it over the open-time thread target. Annotation-only
    # here (no value) so dataclass subclasses that redeclare it keep their own
    # defaults; `capture_reply` assigns it, the concrete states provide the
    # field/default, and `reply_captured` keeps the first one from being overwritten.
    reply_to: str | None
    reply_captured: bool = False
    # Per-run context the timeline feeler injects when it creates the state, so
    # `drive` can present deferred-action batches.
    address: ChannelAddress
    ask_questions: QuestionFeeler
    approvals: ApprovalFeeler
    oauth: OAuthFeeler
    deferred_actions: DeferredActionManager
    subagent_timelines: dict[str, OpenSubagentTimeline] | None = None

    @asynccontextmanager
    async def open_subagent(
        self,
        activity: SubagentActivity,
    ) -> AsyncGenerator[SubagentTimelineState]:
        """Ignore child activity when this timeline has no streaming renderer."""
        yield SubagentTimelineState()

    async def observe_subagent_part(
        self,
        part: ToolCallPart | ToolReturnPart | RetryPromptPart,
    ) -> None:
        if isinstance(part, ToolCallPart):
            if part.tool_name not in {COMMISSION_TOOL_NAME, WHISPER_TOOL_NAME}:
                return
            args = part.args_as_dict()
            name = args.get("name")
            if not isinstance(name, str) or not name:
                logger.warning(
                    "Subagent tool %s has no display name; skipping its timeline",
                    part.tool_call_id,
                )
                return
            activity = SubagentActivity(
                invocation_id=part.tool_call_id,
                kind="commission"
                if part.tool_name == COMMISSION_TOOL_NAME
                else "whisper",
                name=name,
            )
            context = self.open_subagent(activity)
            try:
                state = await context.__aenter__()
            except Exception:
                logger.warning(
                    "Subagent timeline %s failed to open",
                    part.tool_call_id,
                    exc_info=True,
                )
                return
            if self.subagent_timelines is None:
                self.subagent_timelines = {}
            previous = self.subagent_timelines.pop(part.tool_call_id, None)
            if previous is not None:
                await self.settle_subagent(previous, "failed")
            self.subagent_timelines[part.tool_call_id] = OpenSubagentTimeline(
                context=context,
                state=state,
            )
            return

        if self.subagent_timelines is None:
            return
        timeline = self.subagent_timelines.pop(part.tool_call_id, None)
        if timeline is None:
            return
        response = tool_result_text(part)
        if response:
            try:
                await timeline.state.append_response(response)
            except Exception:
                logger.warning(
                    "Subagent timeline %s failed to render its response",
                    part.tool_call_id,
                    exc_info=True,
                )
        status: SubagentActivityStatus = "completed"
        if isinstance(part, RetryPromptPart) or (
            isinstance(part, ToolReturnPart) and part.outcome != "success"
        ):
            status = "failed"
        await self.settle_subagent(timeline, status)

    async def observe_subagent_event(
        self,
        event: StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput],
    ) -> None:
        if isinstance(event, FunctionToolCallEvent | FunctionToolResultEvent):
            try:
                await self.observe_subagent_part(event.part)
            except Exception:
                logger.warning(
                    "Subagent timeline failed to consume event",
                    exc_info=True,
                )

    async def settle_subagent(
        self,
        timeline: OpenSubagentTimeline,
        status: SubagentActivityStatus,
    ) -> None:
        try:
            await timeline.state.settle(status)
        except Exception:
            logger.warning("Subagent timeline failed to settle", exc_info=True)
        try:
            await timeline.context.__aexit__(None, None, None)
        except Exception:
            logger.warning("Subagent timeline failed to close", exc_info=True)

    async def settle_subagents(self, status: SubagentActivityStatus) -> None:
        if self.subagent_timelines is None:
            return
        timelines = list(self.subagent_timelines.values())
        self.subagent_timelines.clear()
        for timeline in timelines:
            await self.settle_subagent(timeline, status)

    async def drive(
        self,
        stream: AsyncIterator[
            StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
        ],
    ) -> None:
        """The single event→render dispatch over the run stream; each typed event is
        drawn onto this state, which renders itself. Draining the whole stream is
        mandatory — abandoning it mid-run tears down the agent task group from the
        wrong task.
        """
        skipped_tools: set[str] = set()
        failed = False
        answered = False
        final_output: ChannelOutput = None
        async for event in stream:
            await self.observe_subagent_event(event)
            if failed:
                continue  # keep draining the stream even after a render failure
            try:
                match event:
                    case PartStartEvent(part=ThinkingPart(content=content)):
                        await self.thinking_start()
                        if content:
                            await self.thinking_delta(content)
                    case PartDeltaEvent(delta=ThinkingPartDelta(content_delta=delta)):
                        await self.thinking_delta(delta or "")
                    case PartEndEvent(part=ThinkingPart()):
                        await self.thinking_end()
                    case PartStartEvent(part=TextPart(content=content)):
                        answered = True
                        await self.answer_start()
                        if content:
                            await self.answer_delta(content)
                    case PartDeltaEvent(delta=TextPartDelta(content_delta=delta)):
                        answered = answered or bool(delta)
                        await self.answer_delta(delta or "")
                    case PartEndEvent(part=TextPart()):
                        await self.answer_end()
                    case FunctionToolCallEvent() | OutputToolCallEvent():
                        if should_skip_plan_tool(event.part.tool_name):
                            if event.part.tool_call_id:
                                skipped_tools.add(event.part.tool_call_id)
                        else:
                            await self.tool_start(event)
                    case FunctionToolResultEvent() | OutputToolResultEvent():
                        part = event.part
                        tool_call_id = getattr(part, "tool_call_id", None)
                        if should_skip_plan_tool(part.tool_name or "") or (
                            tool_call_id is not None and tool_call_id in skipped_tools
                        ):
                            skipped_tools.discard(tool_call_id or "")
                        else:
                            await self.tool_end(event)
                    case ResultSegmentEvent(segment=ReplySegment(data=reply_data)):
                        # The reply handle threads the message; it is not rendered.
                        self.capture_reply(reply_data["id"])
                    case ResultSegmentEvent():
                        answered = True
                        await self.answer_segment(event.segment)
                    case ResultTextDeltaEvent():
                        answered = True
                        await self.answer_delta(event.delta)
                    case (
                        TodoCreatedEvent()
                        | TodoUpdatedEvent()
                        | TodoStatusChangedEvent()
                        | TodoCompletedEvent()
                        | TodoDeletedEvent()
                    ):
                        await self.todo(event)
                    case MessageSentEvent():
                        answered = True
                        await self.message_sent(event)
                    case OAuthAuthorizationEvent():
                        answered = True
                        await self.oauth_authorization(event)
                    case ActionBatchEvent():
                        answered = answered or bool(event.questions or event.approvals)
                        await self.present_actions(event)
                    case AgentRunResultEvent():
                        final_output = event.result.output  # for the fallback below
                    case _:
                        pass  # FinalResult / PartEnd of other parts / passthrough
            except Exception:
                logger.warning(
                    "Channel %s: timeline render failed",
                    self.address.channel_tentacle_id,
                    exc_info=True,
                )
                failed = True
        # `failed` means a render already raised (the transport threw); this
        # "never streamed" backup is outside the try/except, so re-rendering after
        # a failure would propagate and abort the drain (or double-send a partial).
        # It only fires when nothing was drawn — not as an error-recovery retry.
        if (
            not failed
            and not answered
            and isinstance(final_output, Sequence)
            and all(isinstance(segment, Segment) for segment in final_output)
        ):
            reply_to, body = split_reply(cast("list[MessageSegment]", final_output))
            self.capture_reply(reply_to)
            for segment in body:
                await self.answer_segment(segment)
        elif (
            not failed
            and not answered
            and final_output is not None
            and not isinstance(final_output, DeferredToolRequests)
        ):
            # The reply never streamed; render the final output once so the turn
            # is not left blank. A deferral is the exception: the run parked rather
            # than finished, and there is nothing to show for it here — a `teleport`
            # resumes in the place it moved to, and a batch put up for review has
            # already drawn itself through `present_actions`. `str()` on one is its
            # own repr, tool call and metadata and all, which is what this used to
            # post into the chat.
            await self.answer_delta(str(final_output))

    async def present_actions(self, event: ActionBatchEvent) -> None:
        """Render a deferred-action batch as a unit, then record each presented
        action's platform message id."""
        if event.questions:
            message_ids = await self.ask_questions.present(
                self.address,
                event.questions,
            )
            for action in event.questions:
                await self.deferred_actions.mark_action_presented(
                    action.id, message_ids.get(action.id)
                )
        if event.approvals:
            message_ids = await self.approvals.present(self.address, event.approvals)
            for action in event.approvals:
                await self.deferred_actions.mark_action_presented(
                    action.id, message_ids.get(action.id)
                )

    async def actions_presented(self) -> None:
        """A deferred-action batch was just presented into this timeline's
        thread from *outside* its stream — an agent's in-process bridge parking
        its live run on a human. Nothing will flow until the answer, so a
        surface with live status should settle it and say so; the base renders
        no status, so there is nothing to settle."""

    async def begin_entry(self) -> None:
        """A new timeline entry is opening: if answer content streamed since
        the last rotation, that content was a mid-run notice — rotate first."""
        if not self.noticed:
            return
        self.noticed = False
        await self.rotate()

    async def rotate(self) -> None:
        """A mid-run notice happened: finalize the current timeline surface
        (plan message, cards, buffered text) so the notice stays visible where
        it streamed, and let the new entries open a fresh surface below it.
        Work still in flight finishes — and closes — the previous surface."""

    async def thinking_start(self) -> None:
        await self.begin_entry()

    async def thinking_delta(self, text: str) -> None: ...
    async def thinking_end(self) -> None: ...
    async def answer_start(self) -> None: ...

    async def answer_delta(self, text: str) -> None:
        self.noticed = True

    async def answer_end(self) -> None: ...

    async def answer_segment(self, segment: MessageSegment) -> None:
        """One completed message segment from a segment-list output. Platforms
        with a media transport override this to render images/cards natively; the
        default renders the segment's text form."""
        await self.answer_delta(str(segment))

    async def tool_start(
        self, event: FunctionToolCallEvent | OutputToolCallEvent
    ) -> None:
        await self.begin_entry()

    async def tool_end(
        self, event: FunctionToolResultEvent | OutputToolResultEvent
    ) -> None: ...

    async def todo(self, event: TodoEvent) -> None:
        await self.begin_entry()

    def capture_reply(self, reply_to: str | None) -> None:
        """Apply the first reply target seen this run — it overrides the open-time
        thread default; later reply segments are ignored."""
        if reply_to is not None and not self.reply_captured:
            self.reply_to = reply_to
            self.reply_captured = True

    async def message_sent(self, event: MessageSentEvent) -> None:
        """A `send` tool delivered these segments mid-run. Render them as
        reply content (images/cards land natively via `answer_segment`), then
        `begin_entry()` to rotate the just-rendered notice out as its own message —
        the run continues afterward, so the final reply lands separately."""
        reply_to, body = split_reply(event.segments)
        self.capture_reply(reply_to)
        for segment in body:
            await self.answer_segment(segment)
        await self.begin_entry()

    async def oauth_authorization(self, event: OAuthAuthorizationEvent) -> None:
        """An integration is waiting for this user to authorize it.

        Rotates the open entry first: the authorization goes out as its own message
        — a card where the platform has them — so whatever streamed before it stays
        above rather than merging into it.
        """
        await self.begin_entry()
        await self.oauth.present(self.address, event)


class TimelineFeeler(Protocol):
    """Opens a per-run `TimelineState` for a channel. The per-channel `Feelers.timeline`
    is a single stateless instance: `ChannelTentacle.consume` does
    `async with feelers.timeline.open(address) as state:` — entering acquires the platform
    resource and yields the per-run hook (a card, a stream session, …), or the feeler
    itself when there is nothing to set up; `drive_timeline` then renders each event onto
    it; exiting releases the resource and sets `message_id`."""

    def open(
        self, address: ChannelAddress
    ) -> AbstractAsyncContextManager[TimelineState]: ...


def render_todo_lines(todos: Iterable[Todo]) -> list[str]:
    """User-facing checklist lines (`- [x] …` task-list markdown, plaintext-safe):
    an in-progress item shows its active form, subtasks indent under their parent."""
    lines: list[str] = []
    for todo in sorted(todos, key=lambda todo: (todo.position, todo.ref)):
        label = (
            todo.active_form
            if todo.status == "in_progress" and todo.active_form
            else todo.content
        )
        indent = "  " if todo.parent_ref else ""
        lines.append(f"{indent}- {STATUS_MARKERS[todo.status]} {label}")
    return lines


def markdown_from_output(
    output: JsonValue | Sequence[MessageSegment] | DeferredToolRequests,
) -> str | None:
    if output is None or isinstance(output, DeferredToolRequests):
        return None
    if isinstance(output, str):
        return output
    if isinstance(output, Sequence):
        if all(isinstance(item, Segment) for item in output):
            # A leading reply threads the message; it is never rendered as text.
            return (
                "\n\n".join(
                    str(item) for item in output if not isinstance(item, ReplySegment)
                )
                or None
            )
        output = [item for item in output if not isinstance(item, Segment)]
    return f"```json\n{format_stream_value(output)}\n```"


def split_reply(
    segments: Sequence[MessageSegment],
) -> tuple[str | None, list[MessageSegment]]:
    """Take the first `ReplySegment` anywhere in the list as the reply target (its
    `data.id` is passed to the platform as `reply_to`); the body is the remaining
    segments with every reply stripped out, so none renders as `[reply: …]` text.
    No reply segment leaves the list untouched."""
    reply_to: str | None = None
    body: list[MessageSegment] = []
    for seg in segments:
        if isinstance(seg, ReplySegment):
            reply_to = reply_to or seg.data["id"]
        else:
            body.append(seg)
    return reply_to, body


async def present_markdown(
    *,
    ink: Ink[MessageT],
    chromo: Chromo[RawT, MessageT],
    address: ChannelAddress,
    markdown: str,
    reply_to: str | None = None,
) -> IMMessageID | None:
    chat_id = address.chat_id or address.user_id
    chat_type = address.chat_type
    reply_to = reply_to or address.channel_thread_id or None
    first_message_id: IMMessageID | None = None
    with channel_logfire.span(
        "default.markdown.present",
        channel_id=address.channel_tentacle_id,
        conversation_address=str(address),
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
        address: ChannelAddress,
        markdown: str,
    ) -> IMMessageID | None:
        return await present_markdown(
            ink=self.ink,
            chromo=self.chromo,
            address=address,
            markdown=markdown,
        )


class SegmentsFeeler(Protocol):
    """Sends completed output segments to a conversation as one platform message.

    Platforms with native media transport (e.g. NapCat images/files) implement
    this to deliver segments natively; the default joins their text forms.
    """

    async def present(
        self,
        address: ChannelAddress,
        segments: list[MessageSegment],
    ) -> IMMessageID | None: ...


class DefaultSegmentsFeeler(Generic[RawT, MessageT]):
    def __init__(self, *, ink: Ink[MessageT], chromo: Chromo[RawT, MessageT]) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        address: ChannelAddress,
        segments: list[MessageSegment],
    ) -> IMMessageID | None:
        with channel_logfire.span(
            "default.segments.present",
            channel_id=address.channel_tentacle_id,
            conversation_address=str(address),
            segments=len(segments),
        ) as span:
            reply_to, body = split_reply(segments)
            messages = await self.chromo.outbound_segments(body)
            if not messages:
                return None
            chat_id = address.chat_id or address.user_id
            first_message_id: IMMessageID | None = None
            for message in messages:
                message_id = await self.ink.send_message(
                    chat_id,
                    address.chat_type,
                    [message],
                    reply_to or address.channel_thread_id or None,
                )
                first_message_id = first_message_id or message_id
            span.set_attribute("message_id", str(first_message_id))
            return first_message_id


@dataclass
class DefaultTimelineState(TimelineState, Generic[RawT, MessageT]):
    ink: Ink[MessageT]
    chromo: Chromo[RawT, MessageT]
    address: ChannelAddress
    ask_questions: QuestionFeeler
    approvals: ApprovalFeeler
    oauth: OAuthFeeler
    deferred_actions: DeferredActionManager
    parts: list[str] = field(default_factory=list)
    todos: dict[str, Todo] = field(default_factory=dict)
    message_id: IMMessageID | None = None
    reply_to: str | None = None

    async def answer_delta(self, text: str) -> None:
        self.noticed = True
        self.parts.append(text)

    async def todo(self, event: TodoEvent) -> None:
        await self.begin_entry()
        # No live transport: keep the latest snapshot; it rides as a checklist
        # under the final message.
        if isinstance(event, TodoDeletedEvent):
            self.todos.pop(event.todo.ref, None)
        else:
            self.todos[event.todo.ref] = event.todo

    async def rotate(self) -> None:
        # The accumulated text was a mid-run notice: send it as its own message
        # so the final reply arrives separately.
        await self.send_parts()

    async def send_parts(self, *, todo_block: bool = False) -> None:
        markdown = "".join(self.parts)
        self.parts.clear()
        if todo_block and self.todos:
            block = "Tasks:\n" + "\n".join(render_todo_lines(self.todos.values()))
            markdown = f"{markdown}\n\n{block}" if markdown else block
        if not markdown:
            return
        logger.warning(
            "Channel %s: timeline has no streaming transport; "
            "sending the reply as a single message",
            self.address.channel_tentacle_id,
        )
        self.message_id = await present_markdown(
            ink=self.ink,
            chromo=self.chromo,
            address=self.address,
            markdown=markdown,
            reply_to=self.reply_to,
        )


class DefaultTimelineFeeler(TimelineFeeler, Generic[RawT, MessageT]):
    """Answer-only timeline for platforms with no streaming transport (NapCat).

    Stateless; `open(address)` yields a fresh `DefaultTimelineState`. Thinking/tool
    are inherited no-ops; the reply text is accumulated and sent as a single
    message on exit (a mid-run notice rotates out as its own message), with a
    warning that streaming was unavailable.
    """

    def __init__(
        self,
        *,
        ink: Ink[MessageT],
        chromo: Chromo[RawT, MessageT],
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        oauth: OAuthFeeler,
        deferred_actions: DeferredActionManager,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.oauth = oauth
        self.deferred_actions = deferred_actions

    @asynccontextmanager
    async def open(
        self, address: ChannelAddress
    ) -> AsyncGenerator[DefaultTimelineState[RawT, MessageT]]:
        state = DefaultTimelineState(
            ink=self.ink,
            chromo=self.chromo,
            address=address,
            ask_questions=self.ask_questions,
            approvals=self.approvals,
            oauth=self.oauth,
            deferred_actions=self.deferred_actions,
        )
        try:
            yield state
        except asyncio.CancelledError:
            await state.settle_subagents("cancelled")
            raise
        finally:
            await state.settle_subagents("failed")
            await state.send_parts(todo_block=True)
