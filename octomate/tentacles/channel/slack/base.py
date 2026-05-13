from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import SecretStr
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

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
        bot_token: SecretStr,
        app_token: SecretStr,
        agent_id: str = "inkling",
        mention_only: bool = True,
    ) -> None:
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
