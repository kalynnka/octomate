from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.tools import DeferredToolRequests
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

from octomate.config import SlackChannelConfig
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.stream import TextStreamBatcher
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import (
    SlackAssistantThreadEvent,
    SlackMessageEvent,
    SlackOutboundMessage,
)

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

IGNORED_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
    }
)


class SlackTentacle(ChannelTentacle):
    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        config: SlackChannelConfig,
    ) -> None:
        self.app_id = config.app_id
        self.ink = SlackInk(config.bot_token)
        self.chromo = SlackChromo()
        self.app = AsyncApp(token=config.bot_token.get_secret_value())
        self.app.event("message")(self.on_message)
        self.app.event("assistant_thread_started")(self.on_assistant_thread_started)
        self.app.event("assistant_thread_context_changed")(
            self.on_assistant_thread_context_changed
        )
        self.app_token = config.app_token
        self.handler: AsyncSocketModeHandler | None = None
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            config=config,
        )

    async def activate(self) -> None:
        logger.info("Channel %s: starting Slack Socket Mode client", self.id)
        self.handler = AsyncSocketModeHandler(
            self.app,
            self.app_token.get_secret_value(),
        )
        await self.handler.start_async()

    async def deactivate(self) -> None:
        if self.handler:
            await self.handler.close_async()
            self.handler = None

    async def on_message(self, event: SlackMessageEvent, say: AsyncSay) -> None:
        subtype = event.get("subtype")
        if subtype in IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self.profile.user_id:
            return
        await self.ingest(event)

    async def on_assistant_thread_started(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        await self.ensure_assistant_thread(event)

    async def on_assistant_thread_context_changed(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        await self.ensure_assistant_thread(event)

    async def ensure_assistant_thread(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        thread = event.get("assistant_thread", {})
        channel_id = thread.get("channel_id", "")
        thread_ts = thread.get("thread_ts", "")
        user_id = thread.get("user_id", "")
        if not channel_id or not thread_ts or not user_id:
            logger.debug(
                "Channel %s: ignored incomplete Slack assistant thread event %s",
                self.id,
                event,
            )
            return
        if self.octomate is None:
            raise RuntimeError(f"channel {self.id!r} is not attached to Octomate")

        key = ConversationKey(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=channel_id,
            user_id=user_id,
            thread_id=thread_ts,
        )
        with sqlalchemy_materia():
            await self.octomate.conversations.ensure(
                key,
                agent_tentacle_id=self.agent_id,
            )
        logger.info("Channel %s: ensured Slack assistant thread %s", self.id, key)

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        await super().respond(key, events, source_events=source_events)

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        context = self.chromo.thread_context(key, source_events)
        channel = key.chat_id or key.user_id
        final_messages: list[SlackOutboundMessage] = []
        result_event: AgentRunResultEvent[Any] | None = None
        batcher = TextStreamBatcher(
            flush_interval=self.config.stream.flush_interval,
            min_chars=self.config.stream.min_chars,
            max_chars=self.config.stream.max_chars,
            fold_threshold=self.config.stream.fold_threshold,
        )

        try:
            async with self.ink.open_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            ) as stream:
                appended = False
                async for event in events:
                    if result_event is not None:
                        continue

                    delta = self.chromo.render_stream_delta(event)
                    if delta:
                        for update in batcher.push_text(delta):
                            logger.debug(
                                "Channel %s: streaming Slack delta chars=%d sequence=%d",
                                self.id,
                                len(update.delta_text),
                                update.sequence,
                            )
                            await self.ink.append_stream(stream, update.delta_text)
                            appended = True

                    if isinstance(event, AgentRunResultEvent):
                        result_event = event

                is_deferred_result = result_event is not None and isinstance(
                    result_event.result.output,
                    DeferredToolRequests,
                )
                if result_event is not None and not is_deferred_result:
                    final_messages = self.chromo.squirt(result_event.result)
                    final_text = "\n".join(
                        message.markdown_text or message.text
                        for message in final_messages
                    )
                    logger.debug(
                        "Channel %s: Slack stream result chars=%d",
                        self.id,
                        len(final_text),
                    )
                for update in batcher.finish_all():
                    logger.debug(
                        "Channel %s: streaming Slack delta chars=%d sequence=%d",
                        self.id,
                        len(update.delta_text),
                        update.sequence,
                    )
                    await self.ink.append_stream(stream, update.delta_text)
                    appended = True
                if (
                    result_event is not None
                    and not is_deferred_result
                    and final_messages
                    and not appended
                ):
                    for message in final_messages:
                        await self.ink.append_stream(
                            stream,
                            message.markdown_text or message.text,
                        )
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.id,
                exc_info=True,
            )
            if final_messages:
                await self.ink.send_message(
                    channel,
                    key.chat_type,
                    final_messages,
                    context.thread_ts,
                )
