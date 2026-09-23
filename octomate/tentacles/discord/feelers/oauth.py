"""Discord's message for a pending OAuth authorization.

A link button opens the provider's page; a device flow also shows the one-time
code to enter there.
"""

from __future__ import annotations

import discord

from octomate.capabilities.harness.events import (
    LinkProfileAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.oauth import (
    AuthorizationEvent,
    OAuthFeeler,
    link_profile_body,
)
from octomate.tentacles.feelers.output import IMMessageID


class DiscordOAuthFeeler(OAuthFeeler[DiscordOutboundMessage]):
    """Sends the authorization as a message with a link button."""

    @channel_logfire.instrument("discord.oauth.send", extract_args=False)
    async def send(
        self,
        address: ChannelAddress,
        event: AuthorizationEvent,
    ) -> IMMessageID | None:
        if isinstance(event, LinkProfileAuthorizationEvent):
            label = event.host[:100]
            content = f"**{label} authorization**\n{link_profile_body(event)}"
            button_label = f"Continue in {label}"
            authorization_uri = str(event.authorization.authorization_uri)
        else:
            label = event.label[:100]
            button_label = f"Open {label}"
            authorization_uri = event.authorization_uri
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
                label=button_label[:80],
                style=discord.ButtonStyle.link,
                url=authorization_uri,
            )
        )
        chat_id = address.chat_id or address.user_id
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            [DiscordOutboundMessage(content=content, view=view)],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
