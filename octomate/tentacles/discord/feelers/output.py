from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from octomate.capabilities.harness.events import (
    SubagentActivity,
    SubagentActivityStatus,
)
from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    AtSegment,
    FileSegment,
    ImageSegment,
    MessageSegment,
)
from octomate.tentacles.discord.chromo import DiscordChromo
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.output import (
    DefaultTimelineFeeler,
    IMMessageID,
    StreamFlusher,
    SubagentTimelineState,
    TextStreamBatcher,
    TimelineFeeler,
    TimelineState,
)

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager
    from octomate.tentacles.feelers.deferred import (
        ApprovalFeeler,
        QuestionFeeler,
    )
    from octomate.tentacles.feelers.oauth import OAuthFeeler

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
DISCORD_STREAM_PLACEHOLDER = "…"


@dataclass
class DiscordTimelineState(TimelineState):
    ink: DiscordInk
    chromo: DiscordChromo
    address: ChannelAddress
    chat_id: str
    chat_type: str
    channel_thread_id: str
    answer_batcher: TextStreamBatcher
    ask_questions: QuestionFeeler
    approvals: ApprovalFeeler
    oauth: OAuthFeeler
    deferred_actions: DeferredActionManager

    message_id: IMMessageID | None = None
    reply_to: str | None = None
    current_message_id: IMMessageID | None = None
    last_message_id: IMMessageID | None = None
    current_sent_len: int = 0
    current_mentioned_user_ids: list[str] = field(default_factory=list)
    sent_any_message: bool = False
    render_error: Exception | None = None
    text_flusher: StreamFlusher = field(init=False)

    def __post_init__(self) -> None:
        self.text_flusher = StreamFlusher(self.flush_text)

    @asynccontextmanager
    async def open_subagent(
        self,
        activity: SubagentActivity,
    ) -> AsyncGenerator[DiscordSubagentTimelineState]:
        state = DiscordSubagentTimelineState(
            ink=self.ink,
            chromo=self.chromo,
            activity=activity,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            channel_thread_id=self.channel_thread_id,
        )
        await state.start()
        yield state

    async def start_message(self) -> None:
        if self.current_message_id is not None:
            return
        message_id = await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [DiscordOutboundMessage(content=DISCORD_STREAM_PLACEHOLDER)],
            channel_thread_id=self.channel_thread_id,
            reply_to=self.reply_to if not self.sent_any_message else None,
        )
        if message_id is None:
            raise RuntimeError("failed to create Discord streaming message")
        self.current_message_id = message_id
        self.last_message_id = message_id
        self.sent_any_message = True

    async def append_text(
        self,
        text: str,
        *,
        mentioned_user_id: str | None = None,
    ) -> None:
        if not text:
            return
        self.noticed = True

        remaining_text = text
        while remaining_text:
            await self.start_message()
            current_length = len(self.answer_batcher.full_text())
            remaining_capacity = DISCORD_MESSAGE_LIMIT - current_length
            if remaining_capacity == 0 or (
                mentioned_user_id is not None
                and current_length > 0
                and len(remaining_text) > remaining_capacity
            ):
                await self.finish_text()
                if self.render_error is not None:
                    raise self.render_error
                continue

            chunk = remaining_text[:remaining_capacity]
            remaining_text = remaining_text[len(chunk) :]
            if (
                mentioned_user_id is not None
                and mentioned_user_id not in self.current_mentioned_user_ids
            ):
                self.current_mentioned_user_ids.append(mentioned_user_id)
            if self.answer_batcher.push_text(chunk):
                self.text_flusher.signal()
            if remaining_text:
                await self.finish_text()
                if self.render_error is not None:
                    raise self.render_error

    async def answer_delta(self, text: str) -> None:
        await self.append_text(text)

    async def answer_end(self) -> None:
        await self.finish_text()

    async def answer_segment(self, segment: MessageSegment) -> None:
        match segment:
            case AtSegment():
                await self.append_text(
                    f"<@{segment.data.user_id}>",
                    mentioned_user_id=segment.data.user_id,
                )
            case ImageSegment() | FileSegment():
                await self.finish_text()
                if self.render_error is not None:
                    raise self.render_error
                messages = await self.chromo.outbound_segments([segment])
                if not messages:
                    return
                message_id = await self.ink.send_message(
                    self.chat_id,
                    self.chat_type,
                    messages,
                    channel_thread_id=self.channel_thread_id,
                    reply_to=self.reply_to if not self.sent_any_message else None,
                )
                if message_id is None:
                    raise RuntimeError("failed to send Discord output segment")
                self.last_message_id = message_id
                self.sent_any_message = True
            case _:
                await self.answer_delta(str(segment))

    async def flush_text(self) -> None:
        if self.render_error is not None or self.current_message_id is None:
            return
        text = self.answer_batcher.full_text()
        if len(text) <= self.current_sent_len:
            return
        message_id = self.current_message_id
        mentioned_user_ids = tuple(self.current_mentioned_user_ids)
        self.current_sent_len = len(text)
        try:
            await self.ink.edit_message(
                self.channel_thread_id,
                message_id,
                text,
                mentioned_user_ids=mentioned_user_ids,
            )
        except Exception as error:
            self.render_error = error
            logger.warning("Discord timeline: failed to edit message", exc_info=True)

    async def finish_text(self) -> None:
        if self.current_message_id is None:
            return
        self.answer_batcher.finish_all()
        await self.text_flusher.drain()
        await self.flush_text()
        render_error = self.render_error
        try:
            if render_error is not None:
                message_id = await self.ink.send_message(
                    self.chat_id,
                    self.chat_type,
                    [
                        DiscordOutboundMessage(
                            content=self.answer_batcher.full_text(),
                            mentioned_user_ids=tuple(self.current_mentioned_user_ids),
                        )
                    ],
                    channel_thread_id=self.channel_thread_id,
                )
                if message_id is None:
                    raise render_error
                self.last_message_id = message_id
                self.render_error = None
        finally:
            self.current_message_id = None
            self.current_sent_len = 0
            self.current_mentioned_user_ids.clear()
            self.answer_batcher.reset()

    async def rotate(self) -> None:
        await self.finish_text()

    async def actions_presented(self) -> None:
        await self.finish_text()

    async def finish(self) -> None:
        await self.finish_text()
        self.message_id = self.last_message_id


class DiscordTimelineFeeler(TimelineFeeler):
    def __init__(
        self,
        *,
        ink: DiscordInk,
        chromo: DiscordChromo,
        stream_config: ChannelStreamConfig,
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        oauth: OAuthFeeler,
        deferred_actions: DeferredActionManager,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.stream_config = stream_config
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.oauth = oauth
        self.deferred_actions = deferred_actions
        self.clock = clock
        self.default = DefaultTimelineFeeler(
            ink=ink,
            chromo=chromo,
            ask_questions=ask_questions,
            approvals=approvals,
            oauth=oauth,
            deferred_actions=deferred_actions,
        )

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[TimelineState]:
        chat_id = address.chat_id or address.user_id
        channel_thread_id = address.channel_thread_id or chat_id
        async with self.ink.typing(channel_thread_id):
            if not self.stream_config.enabled:
                async with self.default.open(address) as state:
                    yield state
                return

            state = DiscordTimelineState(
                ink=self.ink,
                chromo=self.chromo,
                address=address,
                chat_id=chat_id,
                chat_type=address.chat_type,
                channel_thread_id=channel_thread_id,
                answer_batcher=TextStreamBatcher(
                    flush_interval=self.stream_config.flush_interval,
                    min_chars=self.stream_config.min_chars,
                    max_chars=self.stream_config.max_chars,
                    fold_threshold=self.stream_config.fold_threshold,
                    clock=self.clock,
                ),
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
                await state.finish()


@dataclass
class DiscordSubagentTimelineState(SubagentTimelineState):
    ink: DiscordInk
    chromo: DiscordChromo
    activity: SubagentActivity
    chat_id: str
    chat_type: str
    channel_thread_id: str

    message_id: IMMessageID | None = None
    response: str = ""
    status: SubagentActivityStatus | None = None

    async def start(self) -> None:
        self.message_id = await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [
                DiscordOutboundMessage(
                    content=f"**Subagent · {self.activity.name}**\nStarting…"
                )
            ],
            channel_thread_id=self.channel_thread_id,
        )
        if self.message_id is None:
            raise RuntimeError("failed to create Discord subagent message")

    async def append_response(self, delta: str) -> None:
        if self.status is None and delta:
            self.response += delta

    async def settle(
        self,
        status: SubagentActivityStatus,
        detail: str | None = None,
    ) -> None:
        if self.status is not None or self.message_id is None:
            return
        self.status = status
        if detail:
            self.response = f"{self.response}\n\n{detail}" if self.response else detail
        terminal = {
            "completed": "Completed",
            "failed": "Failed",
            "timed_out": "Timed out",
            "cancelled": "Cancelled",
        }[status]
        content = f"**Subagent · {self.activity.name} — {terminal}**"
        if self.response:
            content = f"{content}\n\n{self.response}"
        messages = self.chromo.outbound_markdown(content)
        first, *remaining = messages
        await self.ink.edit_message(
            self.channel_thread_id,
            self.message_id,
            first.content,
        )
        if remaining:
            await self.ink.send_message(
                self.chat_id,
                self.chat_type,
                remaining,
                channel_thread_id=self.channel_thread_id,
            )
