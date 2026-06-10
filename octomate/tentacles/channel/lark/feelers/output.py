from __future__ import annotations

import json
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

from octomate.capabilities.events import ResultSegmentEvent, ResultTextDeltaEvent
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
    truncate_task_detail,
)
from octomate.tentacles.channel.lark.feelers import cards
from octomate.tentacles.channel.lark.chromo import LARK_STREAM_ELEMENT_ID, LarkChromo
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)

THINKING_HEADER = "🧠 Thinking"


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
            self.chromo.outbound_markdown(markdown),
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
        return await self.present_snapshots(
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
                    await apply_update(update)
        except Exception:
            # A mid-stream failure still gets finalized below with what batched.
            pass

        try:
            for update in batcher.finish_all():
                await apply_update(update)
            if card is not None:
                await self.ink.finish_stream_card(card, sequence=last_sequence + 1)
            else:
                markdown = markdown_from_output(output) if output is not None else None
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

    async def present_snapshots(
        self,
        key: ConversationKey,
        snapshots: AsyncIterator[OutputT],
        *,
        final_output: Callable[[], Awaitable[OutputT | None]] | None,
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
            previous_markdown = ""
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
                    await apply_update(update)
        except Exception:
            if final_output is not None:
                output = await final_output()

        try:
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


def answer_stream_card_data() -> str:
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
        "body": {"elements": [cards.markdown("", element_id=LARK_STREAM_ELEMENT_ID)]},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class LarkRunCards:
    """The per-run cards for one agent run. Each thinking block and each tool call
    is its own card — sent on start, then patched into a folded collapsible panel
    once it finishes — and the answer streams into its own card."""

    ink: LarkInk
    chat_id: str
    chat_type: str
    reply_to: str | None
    reply_in_thread: bool
    answer_batcher: TextStreamBatcher

    answer_card: LarkStreamCard | None = None
    answer_message_id: IMMessageID | None = None
    answer_sequence: int = 0
    saw_answer: bool = False

    thinking_card_id: str | None = None
    thinking_text: str = ""
    tool_cards: dict[str, tuple[str | None, str, str]] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)

    async def post(self, card: JsonObject) -> str | None:
        return await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                )
            ],
            self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )

    async def handle(self, event: AgentStreamEvent) -> None:
        if isinstance(event, PartStartEvent):
            if isinstance(event.part, ThinkingPart):
                await self.start_thinking(event.part.content or "")
            elif isinstance(event.part, TextPart):
                await self.add_answer(event.part.content)
        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, ThinkingPartDelta):
                await self.append_thinking(event.delta.content_delta or "")
            elif isinstance(event.delta, TextPartDelta):
                await self.add_answer(event.delta.content_delta)
        elif isinstance(event, FunctionToolCallEvent | OutputToolCallEvent):
            await self.start_tool(event)
        elif isinstance(event, FunctionToolResultEvent | OutputToolResultEvent):
            await self.finish_tool(event)

    async def start_thinking(self, initial: str) -> None:
        await self.fold_thinking()
        self.thinking_text = initial
        self.thinking_card_id = await self.post(
            cards.card_v2([cards.markdown(f"⏳ **{THINKING_HEADER}…**")])
        )

    async def append_thinking(self, text: str) -> None:
        if not text:
            return
        if self.thinking_card_id is None:
            await self.start_thinking(text)
            return
        self.thinking_text += text

    async def fold_thinking(self) -> None:
        if self.thinking_card_id is None:
            return
        card_id = self.thinking_card_id
        body = self.thinking_text.strip() or "_No detail_"
        self.thinking_card_id = None
        self.thinking_text = ""
        folded = cards.card_v2(
            [cards.collapsible_panel(f"✅ {THINKING_HEADER}", [cards.markdown(body)])]
        )
        await self.ink.patch_card(
            card_id, json.dumps(folded, ensure_ascii=False, separators=(",", ":"))
        )

    async def start_tool(
        self, event: FunctionToolCallEvent | OutputToolCallEvent
    ) -> None:
        await self.fold_thinking()
        tool = event.part
        if should_skip_plan_tool(tool.tool_name):
            if tool.tool_call_id:
                self.skipped.add(tool.tool_call_id)
            return
        title = humanize_tool_name(tool.tool_name)
        if isinstance(event, OutputToolCallEvent):
            title = f"Output: {title}"
        args = tool.args_as_dict()
        args_text = format_tool_arguments(tool.tool_name, args) if args else ""
        body = f"🔧 **{title}**"
        if args_text:
            body += f"\n\n**Arguments**\n{args_text}"
        message_id = await self.post(cards.card_v2([cards.markdown(body)]))
        slot = tool.tool_call_id or f"tool-{len(self.tool_cards)}"
        self.tool_cards[slot] = (message_id, title, args_text)

    async def finish_tool(
        self, event: FunctionToolResultEvent | OutputToolResultEvent
    ) -> None:
        part = event.part
        tool_name = part.tool_name or "output"
        tool_call_id = getattr(part, "tool_call_id", None)
        if should_skip_plan_tool(tool_name) or (
            tool_call_id is not None and tool_call_id in self.skipped
        ):
            if tool_call_id:
                self.skipped.discard(tool_call_id)
            return
        entry = self.tool_cards.pop(tool_call_id or "", None)
        message_id, title, args_text = (
            entry if entry is not None else (None, humanize_tool_name(tool_name), "")
        )
        error = isinstance(part, RetryPromptPart)
        sections: list[str] = []
        if args_text:
            sections.append(f"**Arguments**\n{args_text}")
        sections.append(f"**Result**\n{format_tool_result(part)}")
        folded = cards.card_v2(
            [
                cards.collapsible_panel(
                    f"{'❌' if error else '✅'} 🔧 {title}",
                    [cards.markdown("\n\n".join(sections))],
                )
            ]
        )
        payload = json.dumps(folded, ensure_ascii=False, separators=(",", ":"))
        if message_id is not None:
            await self.ink.patch_card(message_id, payload)
        else:
            await self.post(folded)

    async def add_answer(self, text: str | None) -> None:
        if not text:
            return
        await self.fold_thinking()
        for update in self.answer_batcher.push_text(text):
            await self.push_answer(update.full_text)

    async def ensure_answer_card(self) -> LarkStreamCard:
        if self.answer_card is None:
            self.answer_card = await self.ink.create_stream_card(
                answer_stream_card_data(), element_id=LARK_STREAM_ELEMENT_ID
            )
            self.answer_message_id = await self.ink.send_stream_card(
                self.chat_id,
                self.chat_type,
                self.answer_card,
                reply_to=self.reply_to,
                reply_in_thread=self.reply_in_thread,
            )
            if self.answer_message_id is None:
                raise RuntimeError("failed to send Lark answer card")
        return self.answer_card

    async def push_answer(self, full_text: str) -> None:
        card = await self.ensure_answer_card()
        self.answer_sequence += 1
        self.saw_answer = True
        if not await self.ink.update_stream_card(
            card,
            content=full_text,
            sequence=self.answer_sequence,
        ):
            raise RuntimeError("failed to update Lark answer card")

    async def finish(self, result_event: AgentRunResultEvent[OutputT] | None) -> None:
        await self.fold_thinking()
        for update in self.answer_batcher.finish_all():
            await self.push_answer(update.full_text)
        if (
            not self.saw_answer
            and result_event is not None
            and isinstance(result_event.result.output, str)
            and result_event.result.output
        ):
            await self.push_answer(result_event.result.output)
        if self.answer_card is not None:
            self.answer_sequence += 1
            await self.ink.finish_stream_card(
                self.answer_card, sequence=self.answer_sequence
            )


class LarkEventStreamFeeler(Generic[OutputT]):
    """Posts an agent run as a sequence of cards in the thread: each thinking
    block and each tool call is its own card (sent on start, then patched and
    folded once it finishes), and the final answer streams with the typewriter
    in its own card."""

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
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        cards_state = LarkRunCards(
            ink=self.ink,
            chat_id=key.chat_id or key.user_id,
            chat_type=key.chat_type,
            reply_to=reply_to,
            reply_in_thread=reply_to is not None,
            answer_batcher=TextStreamBatcher(
                flush_interval=self.stream_config.flush_interval,
                min_chars=self.stream_config.min_chars,
                max_chars=self.stream_config.max_chars,
                fold_threshold=self.stream_config.fold_threshold,
            ),
        )

        result_event: AgentRunResultEvent[OutputT] | None = None
        failed = False
        # Always drain the event stream: abandoning it mid-run would tear down
        # the agent task group from the wrong task. Per-event failures degrade
        # the run and fall back to a plain message at the end.
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                result_event = event
                continue
            if failed:
                continue
            try:
                await cards_state.handle(event)
            except Exception:
                logger.warning(
                    "Channel %s: failed to render Lark event card",
                    self.channel_id,
                    exc_info=True,
                )
                failed = True

        if not failed:
            try:
                await cards_state.finish(result_event)
            except Exception:
                logger.warning(
                    "Channel %s: failed to finish Lark answer card",
                    self.channel_id,
                    exc_info=True,
                )
                failed = True

        if failed or not cards_state.saw_answer:
            fallback = None
            if result_event is not None and not isinstance(
                result_event.result.output, DeferredToolRequests
            ):
                fallback = markdown_from_output(result_event.result.output)
            if fallback is not None:
                await self.markdown_feeler.present(key, fallback)
        return cards_state.answer_message_id
