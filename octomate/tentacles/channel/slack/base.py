from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import SlackMessageEvent

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
        app_id: str,
        bot_token: SecretStr,
        app_token: SecretStr,
        agent_id: str = "inkling",
        mention_only: bool = True,
    ) -> None:
        self.app_id = app_id
        self.ink = SlackInk(bot_token)
        self.chromo = SlackChromo()
        self.app = AsyncApp(token=bot_token.get_secret_value())
        self.app.event("message")(self.on_message)
        self.app_token = app_token
        self.handler: AsyncSocketModeHandler | None = None
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            agent_id=agent_id,
            mention_only=mention_only,
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

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        context = self.chromo.thread_context(key, source_events)
        if not context.thread_ts:
            await super().respond(key, events, source_events=source_events)
            return

        channel = key.chat_id or key.user_id
        final_text = ""

        try:
            async with self.ink.open_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            ) as stream:
                async for event in events:
                    delta = self.chromo.render_stream_delta(event)
                    if delta:
                        logger.debug(
                            "Channel %s: streaming Slack delta chars=%d",
                            self.id,
                            len(delta),
                        )
                        await stream.append(delta)

                    if isinstance(event, AgentRunResultEvent):
                        final_text = self.chromo.render_result(event.result)
                        logger.debug(
                            "Channel %s: Slack stream result chars=%d",
                            self.id,
                            len(final_text),
                        )
                        stream.final_markdown_text = final_text
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.id,
                exc_info=True,
            )
            if final_text:
                await self.ink.send_message(
                    channel,
                    key.chat_type,
                    [self.chromo.make_message(final_text)],
                    context.thread_ts,
                )
