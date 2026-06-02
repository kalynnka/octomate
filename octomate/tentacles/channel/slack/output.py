from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

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
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.models.messages.chunk import PlanUpdateChunk, TaskUpdateChunk

from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.feelers.output import (
    BatchedTextUpdate,
    IMMessageID,
    JsonValue,
    MarkdownFeeler,
    TextStreamBatcher,
    markdown_from_output,
)
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)
PLAN_TITLE = "Working on request"
MAX_TASK_DETAIL_CHARS = 2000
SKIPPED_PLAN_TOOL_NAMES = frozenset({"ask_questions"})


TASK_TITLES = {
    "answer": "Answer",
    "thinking": "Thinking",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "subagent": "Agent run",
}


@dataclass
class SlackPlanTask:
    index: int
    sections: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    saw_tool_result: bool = False

    @property
    def id(self) -> str:
        return f"round-{self.index}"

    @property
    def title(self) -> str:
        if not self.tool_names:
            return f"Work round {self.index}"
        if len(self.tool_names) == 1:
            return f"Work round {self.index}: {self.tool_names[0]}"
        shown = ", ".join(self.tool_names[:2])
        if len(self.tool_names) > 2:
            shown += f", +{len(self.tool_names) - 2}"
        return f"Work round {self.index}: {shown}"

    @property
    def details(self) -> str | None:
        if not self.sections:
            return None
        return "\n\n".join(self.sections)

    def append_section(self, title: str, text: str) -> str | None:
        if not text:
            return None
        section = f"*{title}*\n{text}"
        self.sections.append(section)
        return section

    def add_tool_name(self, tool_name: str) -> None:
        if tool_name not in self.tool_names:
            self.tool_names.append(tool_name)

    def chunk(
        self,
        *,
        status: str = "in_progress",
        details: str | None = None,
    ) -> TaskUpdateChunk:
        return TaskUpdateChunk(
            id=self.id,
            title=self.title,
            status=status,
            details=details,
        )


@dataclass
class SlackPlanStreamState:
    ink: SlackInk
    stream: Any
    answer_batcher: TextStreamBatcher
    plan_started: bool = False
    active_task: SlackPlanTask | None = None
    task_index: int = 0
    saw_answer: bool = False
    skipped_tool_call_ids: set[str] = field(default_factory=set)

    async def ensure_plan_started(self) -> None:
        if self.plan_started:
            return
        await self.ink.append_stream_chunks(
            self.stream,
            [PlanUpdateChunk(title=PLAN_TITLE)],
        )
        self.plan_started = True

    async def finish_active_task(self) -> None:
        if self.active_task is None:
            return
        await self.ensure_plan_started()
        await self.ink.append_stream_chunks(
            self.stream,
            [self.active_task.chunk(status="complete")],
        )
        self.active_task = None

    async def current_task(self) -> SlackPlanTask:
        if self.active_task is None:
            self.task_index += 1
            self.active_task = SlackPlanTask(index=self.task_index)
        await self.ensure_plan_started()
        return self.active_task

    async def append_task_section(self, title: str, text: str) -> None:
        task = await self.current_task()
        section = task.append_section(title, text)
        if section is None:
            return
        await self.ink.append_stream_chunks(
            self.stream,
            [task.chunk(details=section)],
        )

    async def append_answer_text(self, text: str) -> None:
        if not text:
            return
        await self.finish_active_task()
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
                    async for streamed_output in stream.stream_output(debounce_by=None):
                        output = streamed_output
                        markdown = markdown_from_output(streamed_output)
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
                    output = await stream.get_output()

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
                    for message in self.chromo.squirt(AgentRunResult(markdown)):
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
            state = SlackPlanStreamState(
                ink=self.ink,
                stream=stream,
                answer_batcher=answer_batcher,
            )
            try:
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        result_event = event
                        await state.finish_active_task()
                        continue

                    if isinstance(event, PartStartEvent):
                        if isinstance(event.part, TextPart):
                            await state.append_answer_text(event.part.content)
                        elif isinstance(event.part, ThinkingPart):
                            if (
                                state.active_task is not None
                                and state.active_task.saw_tool_result
                            ):
                                await state.finish_active_task()
                            await state.append_task_section(
                                "Thinking",
                                event.part.content,
                            )
                        continue

                    if isinstance(event, PartDeltaEvent):
                        if isinstance(event.delta, TextPartDelta):
                            await state.append_answer_text(event.delta.content_delta)
                        elif isinstance(event.delta, ThinkingPartDelta):
                            if (
                                state.active_task is not None
                                and state.active_task.saw_tool_result
                            ):
                                await state.finish_active_task()
                            await state.append_task_section(
                                "Thinking",
                                event.delta.content_delta or "",
                            )
                        continue

                    if isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
                        tool = event.part
                        if state.should_skip_tool_call(
                            tool.tool_name,
                            tool.tool_call_id,
                        ):
                            continue
                        if (
                            state.active_task is not None
                            and state.active_task.saw_tool_result
                        ):
                            await state.finish_active_task()
                        task = await state.current_task()
                        title = (
                            f"Output tool: {tool.tool_name}"
                            if isinstance(event, OutputToolCallEvent)
                            else f"Tool call: {tool.tool_name}"
                        )
                        task.add_tool_name(tool.tool_name)
                        section = task.append_section(
                            title,
                            format_tool_arguments(tool.tool_name, tool.args_as_dict()),
                        )
                        if section is None:
                            continue
                        await self.ink.append_stream_chunks(
                            stream,
                            [task.chunk(details=section)],
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
                        task = await state.current_task()
                        title = (
                            f"Output result: {tool_name}"
                            if isinstance(event, OutputToolResultEvent)
                            else f"Tool result: {tool_name}"
                        )
                        task.add_tool_name(tool_name)
                        task.saw_tool_result = True
                        section = task.append_section(
                            title,
                            format_tool_result(part),
                        )
                        if section is None:
                            continue
                        await self.ink.append_stream_chunks(
                            stream,
                            [task.chunk(details=section)],
                        )

                await state.finish_active_task()
                await state.finish_answer()

                if (
                    result_event is not None
                    and not state.saw_answer
                    and isinstance(result_event.result.output, str)
                ):
                    await self.ink.append_stream(stream, result_event.result.output)
                message_id = await self.ink.stop_stream(stream)
            except Exception:
                try:
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


def task_update_chunk(update: BatchedTextUpdate) -> TaskUpdateChunk:
    title = update.title or TASK_TITLES[update.block_type]
    status = slack_task_status(update.status)
    if update.block_type == "answer":
        return TaskUpdateChunk(
            id=update.block_id,
            title=title,
            status=status,
            output=update.full_text,
        )
    return TaskUpdateChunk(
        id=update.block_id,
        title=title,
        status=status,
        details=update.full_text,
    )


def format_tool_arguments(_tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return "_No arguments_"
    return format_slack_fields(args)


def format_tool_result(part: ToolReturnPart | RetryPromptPart) -> str:
    if isinstance(part, RetryPromptPart):
        return truncate_task_detail(part.model_response())

    value = part.model_response_object()
    if not value:
        return "_No result_"
    return format_slack_fields(value)


def format_slack_fields(value: dict[str, Any]) -> str:
    lines = format_mapping_lines(value)
    return truncate_task_detail("\n".join(lines) if lines else "_No details_")


def should_skip_plan_tool(tool_name: str) -> bool:
    return tool_name in SKIPPED_PLAN_TOOL_NAMES


def format_mapping_lines(value: dict[str, Any], *, indent: int = 0) -> list[str]:
    lines: list[str] = []
    pad = "   " * indent
    for key, item in value.items():
        label = format_field_name(key)
        if isinstance(item, dict):
            lines.append(f"{pad}*{label}:*")
            lines.extend(format_mapping_lines(item, indent=indent + 1))
        elif isinstance(item, list) and item and any(
            isinstance(entry, (dict, list)) for entry in item
        ):
            lines.append(f"{pad}*{label}:*")
            lines.extend(format_list_lines(item, indent=indent + 1))
        else:
            lines.append(f"{pad}*{label}:* {format_inline_value(item)}")
    return lines


def format_list_lines(value: list[Any], *, indent: int = 0) -> list[str]:
    lines: list[str] = []
    pad = "   " * indent
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            lines.append(f"{pad}{index}.")
            lines.extend(format_mapping_lines(item, indent=indent + 1))
        elif isinstance(item, list):
            lines.append(f"{pad}{index}. {format_inline_value(item)}")
        else:
            lines.append(f"{pad}{index}. {format_inline_value(item)}")
    return lines


def format_field_name(value: object) -> str:
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Value"


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


def truncate_task_detail(
    text: str,
    *,
    max_chars: int = MAX_TASK_DETAIL_CHARS,
) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 16].rstrip()}\n...[truncated]"


def slack_task_status(status: str) -> str:
    if status == "done":
        return "complete"
    if status == "error":
        return "error"
    return "in_progress"
