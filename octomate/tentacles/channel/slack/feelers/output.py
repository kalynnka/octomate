from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
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
from slack_sdk.models.messages.chunk import PlanUpdateChunk, TaskUpdateChunk

from octomate.capabilities.events import ResultSegmentEvent, ResultTextDeltaEvent
from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.feelers.output import (
    IMMessageID,
    JsonValue,
    MarkdownFeeler,
    TextStreamBatcher,
    format_fields,
    humanize_tool_name,
    markdown_from_output,
    should_skip_plan_tool,
    status_hint,
    truncate_task_detail,
)
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)
PLAN_TITLE = "Working on request"

THINKING_TITLE = "Thinking"
STATUS_THINKING = "Thinking…"
STATUS_WRITING = "Writing the response…"


@dataclass
class SlackStep:
    """One timeline entry: a single thinking block or a single tool call."""

    id: str
    title: str
    sections: list[str] = field(default_factory=list)
    status: str = "in_progress"

    @property
    def details(self) -> str | None:
        body = "\n\n".join(section for section in self.sections if section)
        return body or None

    def append_section(self, title: str, text: str) -> None:
        if text:
            self.sections.append(f"*{title}*\n{text}")

    def chunk(self, *, status: str | None = None) -> TaskUpdateChunk:
        return TaskUpdateChunk(
            id=self.id,
            title=self.title,
            status=status or self.status,
            details=self.details,
        )


@dataclass
class SlackTimelineState:
    """Drives a Slack ``timeline`` stream: one task per thinking block / tool call,
    plus a live ``assistant.threads.setStatus`` hint for the current agent activity."""

    ink: SlackInk
    stream: Any
    channel: str
    thread_ts: str
    answer_batcher: TextStreamBatcher
    plan_started: bool = False
    saw_answer: bool = False
    thinking_seq: int = 0
    active_thinking: SlackStep | None = None
    tool_steps: dict[str, SlackStep] = field(default_factory=dict)
    skipped_tool_call_ids: set[str] = field(default_factory=set)
    last_status: str | None = None

    async def set_status(self, status: str) -> None:
        if status == self.last_status:
            return
        self.last_status = status
        await self.ink.set_assistant_status(self.channel, self.thread_ts, status)

    async def ensure_plan_started(self) -> None:
        if self.plan_started:
            return
        await self.ink.append_stream_chunks(
            self.stream,
            [PlanUpdateChunk(title=PLAN_TITLE)],
        )
        self.plan_started = True

    async def emit(self, step: SlackStep, *, status: str | None = None) -> None:
        await self.ensure_plan_started()
        await self.ink.append_stream_chunks(self.stream, [step.chunk(status=status)])

    async def complete_active_thinking(self) -> None:
        step = self.active_thinking
        if step is None:
            return
        self.active_thinking = None
        step.status = "complete"
        await self.emit(step, status="complete")

    async def start_thinking(self, text: str) -> None:
        await self.complete_active_thinking()
        self.thinking_seq += 1
        step = SlackStep(id=f"thinking-{self.thinking_seq}", title=THINKING_TITLE)
        if text:
            step.sections.append(text)
        self.active_thinking = step
        await self.set_status(STATUS_THINKING)
        await self.emit(step)

    async def append_thinking_delta(self, text: str) -> None:
        if self.active_thinking is None:
            await self.start_thinking(text)
            return
        if not text:
            return
        if self.active_thinking.sections:
            self.active_thinking.sections[-1] += text
        else:
            self.active_thinking.sections.append(text)

    async def start_tool(
        self,
        tool_call_id: str | None,
        title: str,
        args_text: str | None,
        status: str,
    ) -> None:
        await self.complete_active_thinking()
        step = SlackStep(id=tool_call_id or f"tool-{len(self.tool_steps)}", title=title)
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
        await self.emit(step, status=step.status)

    async def complete_pending(self) -> None:
        await self.complete_active_thinking()
        for step in self.tool_steps.values():
            if step.status == "in_progress":
                step.status = "complete"
                await self.emit(step, status="complete")

    async def append_answer_text(self, text: str) -> None:
        if not text:
            return
        await self.complete_active_thinking()
        await self.set_status(STATUS_WRITING)
        self.saw_answer = True
        for update in self.answer_batcher.push_text(text):
            await self.ink.append_stream(self.stream, update.delta_text)

    async def finish_answer(self) -> None:
        for update in self.answer_batcher.finish_all():
            self.saw_answer = True
            await self.ink.append_stream(self.stream, update.delta_text)

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


class SlackMarkdownStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: SlackInk,
        chromo: SlackChromo,
        stream_config: ChannelStreamConfig,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.stream_config = stream_config
        self.markdown_feeler = markdown_feeler
        self.channel_id = channel_id

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, OutputT],
    ) -> IMMessageID | None:
        return await self._present_snapshots(
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
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        output: OutputT | None = None
        message_id: IMMessageID | None = None
        batcher = TextStreamBatcher(
            flush_interval=self.stream_config.flush_interval,
            min_chars=self.stream_config.min_chars,
            max_chars=self.stream_config.max_chars,
            fold_threshold=self.stream_config.fold_threshold,
        )

        try:
            slack_stream = await self.ink.start_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            )
            try:
                appended = False
                async for event in events:
                    if isinstance(event, FinalResult):
                        output = event.output
                        continue
                    delta_text = (
                        event.delta
                        if isinstance(event, ResultTextDeltaEvent)
                        else str(event.segment)
                    )
                    for update in batcher.push_text(delta_text):
                        appended = True
                        await self.ink.append_stream(slack_stream, update.delta_text)

                for update in batcher.finish_all():
                    appended = True
                    await self.ink.append_stream(slack_stream, update.delta_text)

                markdown = markdown_from_output(output) if output is not None else None
                if markdown is not None and not appended:
                    for message in self.chromo.outbound_markdown(markdown):
                        await self.ink.append_stream(
                            slack_stream,
                            message.markdown_text or message.text,
                        )
                message_id = await self.ink.stop_stream(slack_stream)
            except Exception:
                try:
                    message_id = await self.ink.stop_stream(slack_stream)
                finally:
                    raise
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.channel_id,
                exc_info=True,
            )
            if output is not None:
                markdown = markdown_from_output(output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id

    async def _present_snapshots(
        self,
        key: ConversationKey,
        snapshots: AsyncIterator[OutputT],
        *,
        final_output: Callable[[], Awaitable[OutputT | None]] | None,
    ) -> IMMessageID | None:
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        output: OutputT | None = None
        message_id: IMMessageID | None = None
        batcher = TextStreamBatcher(
            flush_interval=self.stream_config.flush_interval,
            min_chars=self.stream_config.min_chars,
            max_chars=self.stream_config.max_chars,
            fold_threshold=self.stream_config.fold_threshold,
        )

        try:
            slack_stream = await self.ink.start_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            )
            try:
                appended = False
                previous_markdown = ""
                try:
                    async for snapshot in snapshots:
                        output = snapshot
                        markdown = markdown_from_output(snapshot)
                        if markdown is None:
                            continue
                        delta_text = (
                            markdown[len(previous_markdown) :]
                            if markdown.startswith(previous_markdown)
                            else markdown
                        )
                        previous_markdown = markdown
                        for update in batcher.push_text(delta_text):
                            logger.debug(
                                "Channel %s: streaming Slack delta chars=%d sequence=%d",
                                self.channel_id,
                                len(update.delta_text),
                                update.sequence,
                            )
                            appended = True
                            await self.ink.append_stream(
                                slack_stream,
                                update.delta_text,
                            )
                except Exception:
                    if final_output is not None:
                        output = await final_output()

                for update in batcher.finish_all():
                    logger.debug(
                        "Channel %s: streaming Slack delta chars=%d sequence=%d",
                        self.channel_id,
                        len(update.delta_text),
                        update.sequence,
                    )
                    appended = True
                    await self.ink.append_stream(slack_stream, update.delta_text)

                markdown = markdown_from_output(output)
                if markdown is not None and not appended:
                    for message in self.chromo.outbound_markdown(markdown):
                        await self.ink.append_stream(
                            slack_stream,
                            message.markdown_text or message.text,
                        )
                message_id = await self.ink.stop_stream(slack_stream)
            except Exception:
                try:
                    message_id = await self.ink.stop_stream(slack_stream)
                finally:
                    raise
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.channel_id,
                exc_info=True,
            )
            if output is not None:
                markdown = markdown_from_output(output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id


class SlackEventStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: SlackInk,
        chromo: SlackChromo,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.markdown_feeler = markdown_feeler
        self.channel_id = channel_id

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
    ) -> IMMessageID | None:
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        result_event: AgentRunResultEvent[OutputT] | None = None
        answer_batcher = TextStreamBatcher(flush_interval=0, min_chars=0)
        message_id: IMMessageID | None = None

        try:
            stream = await self.ink.start_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
                task_display_mode="timeline",
            )
            state = SlackTimelineState(
                ink=self.ink,
                stream=stream,
                channel=channel,
                thread_ts=context.thread_ts,
                answer_batcher=answer_batcher,
            )
            await state.set_status(STATUS_THINKING)
            try:
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        result_event = event
                        await state.complete_pending()
                        continue

                    if isinstance(event, PartStartEvent):
                        if isinstance(event.part, TextPart):
                            await state.append_answer_text(event.part.content)
                        elif isinstance(event.part, ThinkingPart):
                            await state.start_thinking(event.part.content)
                        continue

                    if isinstance(event, PartDeltaEvent):
                        if isinstance(event.delta, TextPartDelta):
                            await state.append_answer_text(event.delta.content_delta)
                        elif isinstance(event.delta, ThinkingPartDelta):
                            await state.append_thinking_delta(
                                event.delta.content_delta or ""
                            )
                        continue

                    if isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
                        tool = event.part
                        if state.should_skip_tool_call(
                            tool.tool_name,
                            tool.tool_call_id,
                        ):
                            continue
                        title = humanize_tool_name(tool.tool_name)
                        if isinstance(event, OutputToolCallEvent):
                            title = f"Output: {title}"
                        args = tool.args_as_dict()
                        await state.start_tool(
                            tool.tool_call_id,
                            title,
                            format_tool_arguments(tool.tool_name, args)
                            if args
                            else None,
                            status_hint(tool.tool_name),
                        )
                        continue

                    if isinstance(
                        event,
                        FunctionToolResultEvent | OutputToolResultEvent,
                    ):
                        part = event.part
                        tool_name = part.tool_name or "output"
                        if state.should_skip_tool_result(
                            tool_name,
                            getattr(part, "tool_call_id", None),
                        ):
                            continue
                        await state.finish_tool(
                            getattr(part, "tool_call_id", None),
                            tool_name,
                            format_tool_result(part),
                            error=isinstance(part, RetryPromptPart),
                        )

                await state.complete_pending()
                await state.finish_answer()

                if (
                    result_event is not None
                    and not state.saw_answer
                    and isinstance(result_event.result.output, str)
                ):
                    await self.ink.append_stream(stream, result_event.result.output)
                message_id = await self.ink.stop_stream(stream)
                await state.set_status("")
            except Exception:
                try:
                    await state.set_status("")
                    message_id = await self.ink.stop_stream(stream)
                finally:
                    raise
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack event details",
                self.channel_id,
                exc_info=True,
            )
            if result_event is not None and not isinstance(
                result_event.result.output,
                DeferredToolRequests,
            ):
                markdown = markdown_from_output(result_event.result.output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := answer_batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id


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
