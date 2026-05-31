from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests

from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.lark.chromo import (
    LARK_STREAM_ELEMENT_ID,
    LarkChromo,
)
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.schema import LarkStreamCard
from octomate.tentacles.channel.feelers.output import (
    BatchedTextUpdate,
    IMMessageID,
    JsonValue,
    MarkdownFeeler,
    TextStreamBatcher,
    markdown_from_output,
    render_stream_event_delta,
)

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)

LARK_EVENT_ANSWER_ELEMENT_ID = "octomate_run_answer"
LARK_EVENT_DETAILS_ELEMENT_ID = "octomate_run_details"

DETAIL_TITLES = {
    "thinking": "Thinking",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "subagent": "Agent run",
}


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
        stream_started = False
        message_id: IMMessageID | None = None
        output: OutputT | None = None

        async def apply_update(update: BatchedTextUpdate) -> None:
            nonlocal card, message_id, stream_started
            if card is None:
                card_data = self.chromo.make_stream_card_data(
                    "",
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                card = await self.ink.create_stream_card(
                    card_data,
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                if card is None:
                    raise RuntimeError("failed to create Lark stream card")
                message_id = await self.ink.send_stream_card(
                    chat_id,
                    key.chat_type,
                    card,
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                )
                if message_id is None:
                    raise RuntimeError("failed to send Lark stream card")
                stream_started = True

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
            markdown = markdown_from_output(output)
            if markdown is not None and not stream_started:
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Lark response",
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


class LarkEventStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: LarkInk,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
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
        batcher = TextStreamBatcher(flush_interval=0, min_chars=0)
        card: LarkStreamCard | None = None
        answer_sequence = 0
        details_sequence = 0
        answer_text = ""
        detail_blocks: dict[str, BatchedTextUpdate] = {}
        result_event: AgentRunResultEvent[OutputT] | None = None
        message_id: IMMessageID | None = None

        async def ensure_card() -> LarkStreamCard:
            nonlocal card, message_id
            if card is not None:
                return card
            new_card = await self.ink.create_stream_card(
                event_stream_card_data(),
                element_id=LARK_EVENT_ANSWER_ELEMENT_ID,
            )
            if new_card is None:
                raise RuntimeError("failed to create Lark event stream card")
            message_id = await self.ink.send_stream_card(
                chat_id,
                key.chat_type,
                new_card,
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            if message_id is None:
                raise RuntimeError("failed to send Lark event stream card")
            card = new_card
            return new_card

        async def update_answer(content: str) -> None:
            nonlocal answer_sequence, answer_text
            active_card = await ensure_card()
            answer_sequence += 1
            answer_text = content
            if not await self.ink.update_stream_card(
                active_card,
                content=answer_text,
                sequence=answer_sequence,
            ):
                raise RuntimeError("failed to update Lark answer stream")

        async def update_details() -> None:
            nonlocal details_sequence
            active_card = await ensure_card()
            details_sequence += 1
            detail_card = LarkStreamCard(
                card_id=active_card.card_id,
                element_id=LARK_EVENT_DETAILS_ELEMENT_ID,
            )
            if not await self.ink.update_stream_card(
                detail_card,
                content=render_detail_blocks(detail_blocks),
                sequence=details_sequence,
            ):
                raise RuntimeError("failed to update Lark detail stream")

        try:
            async for event in events:
                if result_event is not None:
                    continue

                delta = render_stream_event_delta(event)
                if delta is not None and delta.text:
                    for update in batcher.push_text(delta.text, block=delta.block):
                        if update.block_type == "answer":
                            await update_answer(update.full_text)
                        else:
                            detail_blocks[update.block_id] = update
                            await update_details()

                if isinstance(event, AgentRunResultEvent):
                    result_event = event

            for update in batcher.finish_all():
                if update.block_type == "answer":
                    await update_answer(update.full_text)
                else:
                    detail_blocks[update.block_id] = update
                    await update_details()

            if (
                result_event is not None
                and not answer_text
                and isinstance(result_event.result.output, str)
            ):
                await update_answer(result_event.result.output)
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Lark event details",
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
            elif answer_text:
                await self.markdown_feeler.present(key, answer_text)
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
        "header": {"title": {"tag": "plain_text", "content": "Agent run"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "element_id": LARK_EVENT_ANSWER_ELEMENT_ID,
                },
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "plain_text", "content": "Run details"}
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "",
                            "element_id": LARK_EVENT_DETAILS_ELEMENT_ID,
                        }
                    ],
                },
            ]
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def render_detail_blocks(blocks: dict[str, BatchedTextUpdate]) -> str:
    lines: list[str] = []
    for update in blocks.values():
        title = update.title or DETAIL_TITLES.get(update.block_type, "Run detail")
        text = update.full_text
        if update.block_type in {"tool_call", "tool_result"}:
            text = f"```json\n{text}\n```"
        lines.append(f"**{title}**\n\n{text}")
    return "\n\n---\n\n".join(lines)
