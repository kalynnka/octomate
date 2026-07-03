from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    RetryPromptPart,
    ToolReturnPart,
)
from slack_sdk.models.messages.chunk import PlanUpdateChunk, TaskUpdateChunk
from slack_sdk.web.async_chat_stream import AsyncChatStream

from octomate.capabilities.events import (
    MessageSentEvent,
    TodoDeletedEvent,
    TodoEvent,
)
from octomate.config.channels import ChannelStreamConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    AtSegment,
    CardSegment,
    ImageSegment,
    MessageSegment,
)
from octomate.tentacles.channel.feelers.output import (
    IMMessageID,
    TextStreamBatcher,
    TimelineFeeler,
    TimelineState,
    format_fields,
    humanize_tool_name,
    should_skip_plan_tool,
    split_reply,
    status_hint,
    truncate_task_detail,
)
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import SlackBlock, SlackOutboundMessage

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager
    from octomate.tentacles.channel.feelers.deferred import (
        ApprovalFeeler,
        QuestionFeeler,
    )
from octomate.types.todos import TodoStatus

DEFAULT_PLAN_TITLE = "Working..."

# Slack's task display speaks pending/in_progress/complete; blocked todos show as
# pending (no native blocked state).
SLACK_TODO_STATUS: dict[TodoStatus, str] = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "complete",
    "blocked": "pending",
}

THINKING_TITLE = "Thinking"
STATUS_THINKING = "Thinking…"
STATUS_WRITING = "Writing the response…"

# Each task-chunk emit is one chat.appendStream call resending the step's full
# details, so live thinking re-renders are paced rather than sent per delta.
THINKING_FLUSH_INTERVAL = 1.0
TEXT_STREAM_ROTATE_AFTER = 270.0

# "plan" gathers all tasks into one plan block; "timeline" would render each
# task as an independent inline item.
TASK_DISPLAY_MODE = "plan"


@dataclass
class SlackStep:
    """One timeline entry: a single thinking block or a single tool call."""

    id: str
    title: str
    sections: list[str] = field(default_factory=list)
    status: str = "in_progress"
    started_at: float = 0.0
    output: str | None = None

    @property
    def details(self) -> str | None:
        body = "\n\n".join(section for section in self.sections if section)
        return body or None

    def append_section(self, title: str, text: str) -> None:
        if text:
            self.sections.append(f"*{title}*\n{text}")

    def chunk(self, *, status: str | None = None) -> TaskUpdateChunk:
        # Details ride along on every status (kept on completion too) so Slack can
        # collapse a finished task natively while still letting it expand.
        return TaskUpdateChunk(
            id=self.id,
            title=self.title,
            status=status or self.status,
            details=self.details,
            output=self.output,
        )


@dataclass
class SlackTimelineState(TimelineState):
    """Renders a run as an alternating sequence of Slack messages: a ``plan``
    stream that groups the noisy thinking/tool events (each task expanded while
    it runs, folded once it finishes), and a plain streamed message for every
    text part the agent emits. A text part finishes the current plan and posts
    itself as its own message, so the user gets a readable hint between bursts of
    tool work; the next thinking/tool event opens a fresh plan below it. A tool
    still in flight when the plan finishes keeps its (now-folded) plan open until
    its result lands, then that plan closes."""

    ink: SlackInk
    address: ChannelAddress
    channel: str
    chat_type: str
    thread_ts: str
    ask_questions: QuestionFeeler
    approvals: ApprovalFeeler
    deferred_actions: DeferredActionManager
    answer_batcher: TextStreamBatcher
    recipient_user_id: str | None = None
    recipient_team_id: str | None = None
    message_id: IMMessageID | None = None
    thinking_seq: int = 0
    active_thinking: SlackStep | None = None
    thinking_emitted_at: float = 0.0
    tool_steps: dict[str, SlackStep] = field(default_factory=dict)
    todo_steps: dict[str, SlackStep] = field(default_factory=dict)
    skipped_tool_call_ids: set[str] = field(default_factory=set)
    last_status: str | None = None
    step_streams: dict[str, AsyncChatStream] = field(default_factory=dict)
    retired: list[AsyncChatStream] = field(default_factory=list)
    plan_stream: AsyncChatStream | None = None
    text_stream: AsyncChatStream | None = None
    text_stream_started_at: float = 0.0
    text_flush_task: asyncio.Task[None] | None = None
    text_flush_event: asyncio.Event = field(default_factory=asyncio.Event)
    text_flush_closed: bool = False
    pending_text_delta: str = ""
    last_message_id: IMMessageID | None = None

    def reset_text_flush_state(self) -> None:
        self.text_flush_task = None
        self.text_stream = None
        self.text_stream_started_at = 0.0
        self.pending_text_delta = ""
        self.text_flush_closed = False
        self.text_flush_event.clear()

    async def set_status(self, status: str) -> None:
        if status == self.last_status:
            return
        self.last_status = status
        await self.ink.set_assistant_status(self.channel, self.thread_ts, status)

    async def ensure_plan_stream(
        self, *, title: str = DEFAULT_PLAN_TITLE
    ) -> AsyncChatStream:
        # Opening a plan step ends any open text message: the agent moved from
        # talking back to working, so the message it was streaming is complete.
        # The plan header is named after its first task so the collapsed block
        # reads e.g. "Searched repositories" rather than a generic placeholder.
        await self.finish_text()
        if self.plan_stream is None:
            self.plan_stream = await self.ink.start_stream(
                self.channel,
                self.thread_ts,
                recipient_user_id=self.recipient_user_id,
                recipient_team_id=self.recipient_team_id,
                task_display_mode=TASK_DISPLAY_MODE,
            )
            await self.ink.append_stream_chunks(
                self.plan_stream, [PlanUpdateChunk(title=title or DEFAULT_PLAN_TITLE)]
            )
        return self.plan_stream

    async def emit(self, step: SlackStep, *, status: str | None = None) -> None:
        # A step stays on the stream it first rendered to, so in-flight work
        # finishes in its own plan message even after that plan is folded.
        stream = self.step_streams.get(step.id)
        if stream is None:
            stream = await self.ensure_plan_stream(title=step.title)
            self.step_streams[step.id] = stream
        await self.ink.append_stream_chunks(stream, [step.chunk(status=status)])

    def stream_has_inflight(self, stream: AsyncChatStream) -> bool:
        return any(
            step.status == "in_progress" and self.step_streams.get(step.id) is stream
            for step in self.tool_steps.values()
        )

    async def finish_plan(self) -> None:
        """Fold the current plan. If a tool is still running it stays open (and
        retired) so the late result lands where it started; otherwise it closes."""
        plan = self.plan_stream
        if plan is None:
            return
        self.plan_stream = None
        if self.stream_has_inflight(plan):
            self.retired.append(plan)
        else:
            await self.ink.stop_stream(plan)

    async def ensure_text_stream(self) -> AsyncChatStream:
        if self.text_stream is None:
            await self.complete_active_thinking()
            await self.finish_plan()
            await self.set_status(STATUS_WRITING)
            # Each text part is its own Slack message: clear the batcher's carried
            # `full_text` so the previous message's length can't fold this one.
            self.answer_batcher.reset()
            self.reset_text_flush_state()
            self.text_stream = await self.ink.start_stream(
                self.channel,
                self.thread_ts,
                recipient_user_id=self.recipient_user_id,
                recipient_team_id=self.recipient_team_id,
            )
            self.text_stream_started_at = time.monotonic()
            self.text_flush_task = asyncio.create_task(self.flush_text_loop())
        return self.text_stream

    async def flush_text_loop(self) -> None:
        while True:
            await self.text_flush_event.wait()
            self.text_flush_event.clear()
            while self.pending_text_delta:
                markdown_text = self.pending_text_delta
                self.pending_text_delta = ""
                stream = self.text_stream
                if stream is None:
                    raise RuntimeError("Slack text stream is not open")
                if (
                    time.monotonic() - self.text_stream_started_at
                    >= TEXT_STREAM_ROTATE_AFTER
                ):
                    self.text_stream = None
                    await self.ink.stop_stream(stream)
                    stream = await self.ink.start_stream(
                        self.channel,
                        self.thread_ts,
                        recipient_user_id=self.recipient_user_id,
                        recipient_team_id=self.recipient_team_id,
                    )
                    self.text_stream = stream
                    self.text_stream_started_at = time.monotonic()
                await self.ink.append_stream(stream, markdown_text)
            if self.text_flush_closed:
                return

    async def finish_text(self) -> None:
        if self.text_stream is None:
            return
        for update in self.answer_batcher.finish_all():
            self.pending_text_delta += update.delta_text
            self.text_flush_event.set()
        self.text_flush_closed = True
        self.text_flush_event.set()
        task = self.text_flush_task
        stream: AsyncChatStream | None = None
        try:
            if task is not None:
                await task
            stream = self.text_stream
        finally:
            self.reset_text_flush_state()
        if stream is not None:
            self.last_message_id = await self.ink.stop_stream(stream)

    async def release_drained(self) -> None:
        for stream in [s for s in self.retired if not self.stream_has_inflight(s)]:
            self.retired.remove(stream)
            await self.ink.stop_stream(stream)

    async def complete_active_thinking(self) -> None:
        step = self.active_thinking
        if step is None:
            return
        self.active_thinking = None
        elapsed = max(1, round(time.monotonic() - step.started_at))
        step.title = f"Thought for {elapsed}s"
        step.status = "complete"
        await self.emit(step, status="complete")

    async def thinking_start(self) -> None:
        await self.complete_active_thinking()
        self.thinking_seq += 1
        step = SlackStep(
            id=f"thinking-{self.thinking_seq}",
            title=THINKING_TITLE,
            started_at=time.monotonic(),
        )
        self.active_thinking = step
        await self.set_status(STATUS_THINKING)
        await self.emit(step)
        self.thinking_emitted_at = time.monotonic()

    async def thinking_delta(self, text: str) -> None:
        if self.active_thinking is None:
            await self.thinking_start()
        step = self.active_thinking
        if step is None or not text:
            return
        if step.sections:
            step.sections[-1] += text
        else:
            step.sections.append(text)
        now = time.monotonic()
        if now - self.thinking_emitted_at >= THINKING_FLUSH_INTERVAL:
            self.thinking_emitted_at = now
            await self.emit(step)

    async def start_tool(
        self,
        tool_call_id: str | None,
        title: str,
        args_text: str | None,
        status: str,
        *,
        tool_name: str | None = None,
    ) -> None:
        await self.complete_active_thinking()
        step = SlackStep(
            id=tool_call_id or f"tool-{len(self.tool_steps)}",
            title=title,
            started_at=time.monotonic(),
        )
        # The body leads with the exact tool id, so an expanded task names the
        # precise tool the friendly title was built from.
        if tool_name:
            step.append_section("Tool", f"`{tool_name}`")
        if args_text:
            step.append_section("Arguments", args_text)
        self.tool_steps[step.id] = step
        await self.set_status(status)
        await self.emit(step)

    async def finish_tool(
        self,
        tool_call_id: str | None,
        tool_name: str,
        result_text: str,
        *,
        error: bool,
    ) -> None:
        step = self.tool_steps.get(tool_call_id or "")
        if step is None:
            step = SlackStep(
                id=tool_call_id or f"tool-{len(self.tool_steps)}",
                title=humanize_tool_name(tool_name),
            )
            self.tool_steps[step.id] = step
        step.append_section("Result", result_text)
        step.status = "error" if error else "complete"
        if error:
            # A folded error row keeps a one-line trace so failures stay visible.
            stripped = result_text.strip()
            step.output = stripped.splitlines()[0][:200] if stripped else "Failed"
        await self.emit(step, status=step.status)
        await self.release_drained()

    async def tool_start(
        self, event: FunctionToolCallEvent | OutputToolCallEvent
    ) -> None:
        tool = event.part
        args = tool.args_as_dict()
        title = humanize_tool_name(tool.tool_name)
        if isinstance(event, OutputToolCallEvent):
            title = f"Output: {title}"
        await self.start_tool(
            tool.tool_call_id,
            title,
            format_tool_arguments(tool.tool_name, args) if args else None,
            status_hint(tool.tool_name),
            tool_name=tool.tool_name,
        )

    async def tool_end(
        self, event: FunctionToolResultEvent | OutputToolResultEvent
    ) -> None:
        part = event.part
        await self.finish_tool(
            getattr(part, "tool_call_id", None),
            part.tool_name or "output",
            format_tool_result(part),
            error=isinstance(part, RetryPromptPart),
        )

    async def complete_pending(self) -> None:
        await self.complete_active_thinking()
        for step in self.tool_steps.values():
            if step.status == "in_progress":
                step.status = "complete"
                await self.emit(step, status="complete")

    async def answer_start(self) -> None:
        # A text part begins: finish the current plan and open its own message.
        await self.ensure_text_stream()

    async def answer_delta(self, text: str) -> None:
        if not text:
            return
        await self.ensure_text_stream()
        for update in self.answer_batcher.push_text(text):
            self.pending_text_delta += update.delta_text
            self.text_flush_event.set()

    async def answer_end(self) -> None:
        await self.finish_text()

    async def message_sent(self, event: MessageSentEvent) -> None:
        # A `send_message` delivery is output too: render its segments as a
        # discrete message and close it, rather than folding it into a plan.
        reply_to, body = split_reply(event.segments)
        self.capture_reply(reply_to)
        for segment in body:
            await self.answer_segment(segment)
        await self.finish_text()

    async def answer_segment(self, segment: MessageSegment) -> None:
        match segment:
            case AtSegment():
                await self.answer_delta(f"<@{segment.data.user_id}>")
            case ImageSegment():
                await self.complete_active_thinking()
                await self.finish_text()
                await self.finish_plan()
                await self.ink.upload_image(
                    channel=self.channel,
                    data=segment.data.path.read_bytes(),
                    filename=segment.data.name or segment.data.path.name,
                    thread_ts=self.thread_ts,
                )
            case CardSegment():
                await self.complete_active_thinking()
                await self.finish_text()
                await self.finish_plan()
                payload_blocks = segment.data.payload.get("blocks")
                if not isinstance(payload_blocks, list):
                    raise ValueError("Slack card payload requires a 'blocks' list")
                blocks: list[SlackBlock] = []
                for block in payload_blocks:
                    if not isinstance(block, dict):
                        raise ValueError("Slack card blocks must be objects")
                    blocks.append(block)
                await self.ink.send_message(
                    self.channel,
                    self.chat_type,
                    [SlackOutboundMessage(text="[card]", blocks=blocks)],
                    self.thread_ts,
                )
            case _:
                await self.answer_delta(str(segment))

    async def todo(self, event: TodoEvent) -> None:
        todo = event.todo
        # Todos are live plan state: they always render in the current plan,
        # opening a fresh one if the previous plan was already folded.
        plan = await self.ensure_plan_stream(title=todo.content)
        if isinstance(event, TodoDeletedEvent):
            step = self.todo_steps.pop(todo.ref, None)
            if step is not None:
                self.step_streams[step.id] = plan
                step.status = "complete"
                step.output = "Removed from the plan"
                await self.emit(step)
            return
        step = self.todo_steps.get(todo.ref)
        if step is None:
            step = SlackStep(id=f"todo-{todo.ref}", title=todo.content)
            self.todo_steps[todo.ref] = step
        self.step_streams[step.id] = plan
        step.title = (
            todo.active_form
            if todo.status == "in_progress" and todo.active_form
            else todo.content
        )
        step.status = SLACK_TODO_STATUS[todo.status]
        await self.emit(step)

    def should_skip_tool_call(self, tool_name: str, tool_call_id: str | None) -> bool:
        if not should_skip_plan_tool(tool_name):
            return False
        if tool_call_id:
            self.skipped_tool_call_ids.add(tool_call_id)
        return True

    def should_skip_tool_result(self, tool_name: str, tool_call_id: str | None) -> bool:
        should_skip = should_skip_plan_tool(tool_name) or (
            bool(tool_call_id) and tool_call_id in self.skipped_tool_call_ids
        )
        if should_skip and tool_call_id:
            self.skipped_tool_call_ids.discard(tool_call_id)
        return should_skip


def format_tool_arguments(_tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return "_No arguments_"
    return format_fields(args)


def format_tool_result(part: ToolReturnPart | RetryPromptPart) -> str:
    if isinstance(part, RetryPromptPart):
        return truncate_task_detail(part.model_response())

    value = part.model_response_object()
    if not value:
        return "_No result_"
    return format_fields(value)


class SlackTimelineFeeler(TimelineFeeler):
    """Renders a run as a Slack `timeline` stream (thinking blocks + tool tasks +
    inline answer), driven event-by-event by `ChannelTentacle.consume`.

    Stateless; `open(address)` starts the `timeline` stream and yields a per-run
    `SlackTimelineState` machine that `drive_timeline` renders each event onto."""

    def __init__(
        self,
        *,
        ink: SlackInk,
        chromo: SlackChromo,
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        deferred_actions: DeferredActionManager,
        stream_config: ChannelStreamConfig,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.deferred_actions = deferred_actions
        self.stream_config = stream_config

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncIterator[SlackTimelineState]:
        context = self.chromo.thread_context(address)
        channel = address.chat_id or address.user_id
        # Streams open lazily: the first thinking/tool event starts a plan, the
        # first text part starts a message. A run with neither sends nothing.
        state = SlackTimelineState(
            ink=self.ink,
            address=address,
            channel=channel,
            chat_type=address.chat_type,
            thread_ts=context.thread_ts,
            ask_questions=self.ask_questions,
            approvals=self.approvals,
            deferred_actions=self.deferred_actions,
            answer_batcher=TextStreamBatcher(
                flush_interval=self.stream_config.flush_interval,
                min_chars=self.stream_config.min_chars,
                max_chars=self.stream_config.max_chars,
                fold_threshold=self.stream_config.fold_threshold,
            ),
            recipient_user_id=context.recipient_user_id,
            recipient_team_id=context.recipient_team_id,
        )
        await state.set_status(STATUS_THINKING)
        try:
            yield state
        finally:
            await state.complete_pending()
            await state.finish_text()
            await state.finish_plan()
            await state.release_drained()
            # The last text message is the final reply.
            state.message_id = state.last_message_id
            await state.set_status("")
