from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

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
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests

from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.feelers.output import (
    BatchedTextUpdate,
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
from octomate.tentacles.channel.lark.chromo import LARK_STREAM_ELEMENT_ID, LarkChromo
from octomate.tentacles.channel.lark.feelers import cards
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.schema import LarkStreamCard

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)

# Lark caps element ids at 20 chars (alphanumeric + underscore, alpha start).
LARK_EVENT_STATUS_ELEMENT_ID = "octomate_run_status"
LARK_EVENT_ANSWER_ELEMENT_ID = "octomate_run_answer"
LARK_EVENT_TIMELINE_ELEMENT_ID = "octomate_run_steps"
# Back-compat alias for the prior single "Run details" element id.
LARK_EVENT_DETAILS_ELEMENT_ID = LARK_EVENT_TIMELINE_ELEMENT_ID

THINKING_TITLE = "Thinking"
STATUS_THINKING = "⏳ Thinking…"
STATUS_WRITING = "✍️ Writing…"
STATUS_DONE = "✅ Done"


def format_tool_arguments(_tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return "_No arguments_"
    return format_fields(args, bold="**")


def format_tool_result(part: ToolReturnPart | RetryPromptPart) -> str:
    if isinstance(part, RetryPromptPart):
        return truncate_task_detail(part.model_response())
    value = part.model_response_object()
    if not value:
        return "_No result_"
    return format_fields(value, bold="**")


class LarkMarkdownFeeler:
    def __init__(self, *, ink: LarkInk, chromo: LarkChromo) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> IMMessageID | None:
        chat_id = key.chat_id or key.user_id
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        return await self.ink.send_message(
            chat_id,
            key.chat_type,
            [self.chromo.make_markdown_message(markdown)],
            reply_to,
            reply_in_thread=reply_to is not None,
        )


class LarkMarkdownStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: LarkInk,
        chromo: LarkChromo,
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
        chat_id = key.chat_id or key.user_id
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        reply_in_thread = reply_to is not None
        batcher = TextStreamBatcher(
            flush_interval=self.stream_config.flush_interval,
            min_chars=self.stream_config.min_chars,
            max_chars=self.stream_config.max_chars,
            fold_threshold=self.stream_config.fold_threshold,
        )
        card: LarkStreamCard | None = None
        message_id: IMMessageID | None = None
        output: OutputT | None = None
        last_sequence = 0

        async def apply_update(update: BatchedTextUpdate) -> None:
            nonlocal card, message_id, last_sequence
            if card is None:
                card_data = self.chromo.make_stream_card_data(
                    "",
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                card = await self.ink.create_stream_card(
                    card_data,
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                message_id = await self.ink.send_stream_card(
                    chat_id,
                    key.chat_type,
                    card,
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                )
                if message_id is None:
                    raise RuntimeError("failed to send Lark stream card")

            last_sequence = update.sequence
            if not await self.ink.update_stream_card(
                card,
                content=update.full_text,
                sequence=update.sequence,
            ):
                raise RuntimeError("failed to update Lark stream card")

        try:
            try:
                previous_markdown = ""
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
                        await apply_update(update)
            except Exception:
                output = await stream.get_output()

            for update in batcher.finish_all():
                await apply_update(update)

            if card is not None:
                await self.ink.finish_stream_card(card, sequence=last_sequence + 1)
            else:
                markdown = markdown_from_output(output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Lark response",
                self.channel_id,
                exc_info=True,
            )
            if card is not None:
                await self.ink.finish_stream_card(card, sequence=last_sequence + 1)
            if output is not None:
                markdown = markdown_from_output(output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id


@dataclass
class LarkStep:
    """One timeline entry: a single thinking block or a single tool call."""

    id: str
    title: str
    status: str = "in_progress"
    sections: list[str] = field(default_factory=list)

    STATUS_EMOJI: ClassVar[dict[str, str]] = {
        "in_progress": "⏳",
        "complete": "✅",
        "error": "❌",
    }

    def append_section(self, title: str, text: str) -> None:
        if text:
            self.sections.append(f"**{title}**\n{text}")

    def render(self) -> str:
        head = f"{self.STATUS_EMOJI.get(self.status, '⏳')} **{self.title}**"
        body = "\n\n".join(section for section in self.sections if section)
        return f"{head}\n\n{body}" if body else head


@dataclass
class LarkTimelineState:
    """Drives a Lark streaming card: the answer element gets the typewriter
    animation, a status element shows the live activity, and a per-step timeline
    snap-updates inside the collapsible "Run details" panel. Every element update
    on the card draws from one monotonic ``sequence`` so the platform never drops
    an out-of-order write."""

    ink: LarkInk
    card: LarkStreamCard
    answer_batcher: TextStreamBatcher
    saw_answer: bool = False
    thinking_seq: int = 0
    active_thinking: LarkStep | None = None
    steps: list[LarkStep] = field(default_factory=list)
    step_index: dict[str, LarkStep] = field(default_factory=dict)
    skipped_tool_call_ids: set[str] = field(default_factory=set)
    last_status: str | None = None
    sequence: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    async def update_element(self, element_id: str, content: str) -> None:
        element = LarkStreamCard(card_id=self.card.card_id, element_id=element_id)
        if not await self.ink.update_stream_card(
            element,
            content=content,
            sequence=self.next_sequence(),
        ):
            raise RuntimeError("failed to update Lark stream card")

    def add_step(self, step: LarkStep) -> None:
        self.steps.append(step)
        self.step_index[step.id] = step

    async def set_status(self, status: str) -> None:
        if status == self.last_status:
            return
        self.last_status = status
        await self.update_element(LARK_EVENT_STATUS_ELEMENT_ID, status)

    async def flush_timeline(self) -> None:
        if not self.steps:
            return
        body = "\n\n---\n\n".join(step.render() for step in self.steps)
        await self.update_element(
            LARK_EVENT_TIMELINE_ELEMENT_ID,
            f"**Run details**\n\n{body}",
        )

    async def complete_active_thinking(self) -> None:
        step = self.active_thinking
        if step is None:
            return
        self.active_thinking = None
        step.status = "complete"
        await self.flush_timeline()

    async def start_thinking(self, text: str) -> None:
        await self.complete_active_thinking()
        self.thinking_seq += 1
        step = LarkStep(id=f"thinking-{self.thinking_seq}", title=THINKING_TITLE)
        if text:
            step.sections.append(text)
        self.active_thinking = step
        self.add_step(step)
        await self.set_status(STATUS_THINKING)
        await self.flush_timeline()

    async def append_thinking_delta(self, text: str) -> None:
        if self.active_thinking is None:
            await self.start_thinking(text)
            return
        if not text:
            return
        # Accumulate only — flushed on the next structural boundary to stay
        # under Lark's per-card update rate limit.
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
        step = LarkStep(id=tool_call_id or f"tool-{len(self.steps)}", title=title)
        if args_text:
            step.append_section("Arguments", args_text)
        self.add_step(step)
        await self.set_status(status)
        await self.flush_timeline()

    async def finish_tool(
        self,
        tool_call_id: str | None,
        tool_name: str,
        result_text: str,
        *,
        error: bool,
    ) -> None:
        step = self.step_index.get(tool_call_id or "")
        if step is None:
            step = LarkStep(
                id=tool_call_id or f"tool-{len(self.steps)}",
                title=humanize_tool_name(tool_name),
            )
            self.add_step(step)
        step.append_section("Result", result_text)
        step.status = "error" if error else "complete"
        await self.flush_timeline()

    async def complete_pending(self) -> None:
        await self.complete_active_thinking()
        changed = False
        for step in self.steps:
            if step.status == "in_progress":
                step.status = "complete"
                changed = True
        if changed:
            await self.flush_timeline()

    async def append_answer_text(self, text: str) -> None:
        if not text:
            return
        await self.complete_active_thinking()
        await self.set_status(STATUS_WRITING)
        self.saw_answer = True
        for update in self.answer_batcher.push_text(text):
            await self.update_element(LARK_EVENT_ANSWER_ELEMENT_ID, update.full_text)

    async def finish_answer(self) -> None:
        for update in self.answer_batcher.finish_all():
            self.saw_answer = True
            await self.update_element(LARK_EVENT_ANSWER_ELEMENT_ID, update.full_text)

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


class LarkEventStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: LarkInk,
        stream_config: ChannelStreamConfig,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
        self.stream_config = stream_config
        self.markdown_feeler = markdown_feeler
        self.channel_id = channel_id

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
    ) -> IMMessageID | None:
        chat_id = key.chat_id or key.user_id
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        reply_in_thread = reply_to is not None
        answer_batcher = TextStreamBatcher(
            flush_interval=self.stream_config.flush_interval,
            min_chars=self.stream_config.min_chars,
            max_chars=self.stream_config.max_chars,
            fold_threshold=self.stream_config.fold_threshold,
        )
        result_event: AgentRunResultEvent[OutputT] | None = None
        message_id: IMMessageID | None = None
        state: LarkTimelineState | None = None

        async def ensure_state() -> LarkTimelineState:
            nonlocal state, message_id
            if state is not None:
                return state
            card = await self.ink.create_stream_card(
                event_stream_card_data(),
                element_id=LARK_EVENT_ANSWER_ELEMENT_ID,
            )
            message_id = await self.ink.send_stream_card(
                chat_id,
                key.chat_type,
                card,
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            if message_id is None:
                raise RuntimeError("failed to send Lark event stream card")
            new_state = LarkTimelineState(
                ink=self.ink,
                card=card,
                answer_batcher=answer_batcher,
            )
            state = new_state
            await new_state.set_status(STATUS_THINKING)
            return new_state

        failed = False
        # Always drain ``events`` fully — abandoning the generator midway leaves
        # the reception stream without a result and tears down the agent task
        # group from the wrong task. So we swallow per-event failures, mark the
        # run degraded, and fall back to a plain message at the end.
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                result_event = event
                if state is not None and not failed:
                    try:
                        await state.complete_pending()
                    except Exception:
                        logger.warning(
                            "Channel %s: failed to stream Lark event details",
                            self.channel_id,
                            exc_info=True,
                        )
                        failed = True
                continue

            if failed:
                continue

            try:
                active = await ensure_state()

                if isinstance(event, PartStartEvent):
                    if isinstance(event.part, TextPart):
                        await active.append_answer_text(event.part.content)
                    elif isinstance(event.part, ThinkingPart):
                        await active.start_thinking(event.part.content)
                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, TextPartDelta):
                        await active.append_answer_text(event.delta.content_delta)
                    elif isinstance(event.delta, ThinkingPartDelta):
                        await active.append_thinking_delta(
                            event.delta.content_delta or ""
                        )
                elif isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
                    tool = event.part
                    if not active.should_skip_tool_call(
                        tool.tool_name, tool.tool_call_id
                    ):
                        title = humanize_tool_name(tool.tool_name)
                        if isinstance(event, OutputToolCallEvent):
                            title = f"Output: {title}"
                        args = tool.args_as_dict()
                        await active.start_tool(
                            tool.tool_call_id,
                            title,
                            format_tool_arguments(tool.tool_name, args)
                            if args
                            else None,
                            f"🔧 {status_hint(tool.tool_name)}",
                        )
                elif isinstance(
                    event, FunctionToolResultEvent | OutputToolResultEvent
                ):
                    part = event.part
                    tool_name = part.tool_name or "output"
                    if not active.should_skip_tool_result(
                        tool_name, getattr(part, "tool_call_id", None)
                    ):
                        await active.finish_tool(
                            getattr(part, "tool_call_id", None),
                            tool_name,
                            format_tool_result(part),
                            error=isinstance(part, RetryPromptPart),
                        )
            except Exception:
                logger.warning(
                    "Channel %s: failed to stream Lark event details",
                    self.channel_id,
                    exc_info=True,
                )
                failed = True

        if state is not None and not failed:
            try:
                await state.complete_pending()
                await state.finish_answer()
                if (
                    result_event is not None
                    and not state.saw_answer
                    and isinstance(result_event.result.output, str)
                ):
                    await state.update_element(
                        LARK_EVENT_ANSWER_ELEMENT_ID,
                        result_event.result.output,
                    )
                await state.set_status(STATUS_DONE)
            except Exception:
                logger.warning(
                    "Channel %s: failed to finish Lark event stream",
                    self.channel_id,
                    exc_info=True,
                )
                failed = True

        if state is not None:
            await self.ink.finish_stream_card(
                state.card,
                sequence=state.next_sequence(),
            )

        if state is None or failed:
            fallback = None
            if result_event is not None and not isinstance(
                result_event.result.output,
                DeferredToolRequests,
            ):
                fallback = markdown_from_output(result_event.result.output)
            if fallback is None and (text := answer_batcher.full_text()):
                fallback = text
            if fallback is not None:
                await self.markdown_feeler.present(key, fallback)
        return message_id


def event_stream_card_data() -> str:
    payload = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": ""},
            "streaming_config": {
                "print_frequency_ms": {
                    "default": 20,
                    "android": 20,
                    "ios": 20,
                    "pc": 20,
                },
                "print_step": {"default": 12, "android": 12, "ios": 12, "pc": 12},
                "print_strategy": "fast",
            },
        },
        "header": cards.header("Agent run"),
        "body": {
            # Plain markdown elements only — Lark rejects/locks streaming cards
            # that contain containers like collapsible_panel.
            "elements": [
                cards.markdown("", element_id=LARK_EVENT_STATUS_ELEMENT_ID),
                cards.markdown("", element_id=LARK_EVENT_ANSWER_ELEMENT_ID),
                cards.markdown("", element_id=LARK_EVENT_TIMELINE_ELEMENT_ID),
            ]
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
