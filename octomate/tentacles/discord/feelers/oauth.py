from __future__ import annotations

import discord

from octomate.capabilities.harness.events import (
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.oauth import OAuthFeeler
from octomate.tentacles.feelers.output import IMMessageID


class DiscordOAuthFeeler(OAuthFeeler[DiscordOutboundMessage]):
    @channel_logfire.instrument("discord.oauth.send", extract_args=False)
    async def send(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None:
        label = event.label[:100]
        if isinstance(event, OAuthDeviceAuthorizationEvent):
            content = (
                f"**Connect {label}**\n"
                f"Enter code `{event.user_code}` on the verification page, then "
                "return here and tell me to confirm."
            )
        else:
            content = (
                f"**Connect {label}**\n"
                "Open the authorization page and approve the request."
            )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=f"Open {label}"[:80],
                style=discord.ButtonStyle.link,
                url=event.authorization_uri,
            )
        )
        chat_id = address.chat_id or address.user_id
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            [DiscordOutboundMessage(content=content, view=view)],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
