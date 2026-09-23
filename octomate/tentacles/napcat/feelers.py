"""NapCat's plain-text OAuth feeler."""

from __future__ import annotations

from octomate.capabilities.harness.events import (
    LinkProfileAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.feelers.oauth import (
    AuthorizationEvent,
    OAuthFeeler,
    link_profile_body,
)
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.napcat.schema import NapcatOutboundMessage


class NapcatOAuthFeeler(OAuthFeeler[NapcatOutboundMessage]):
    """Send literal authorization URLs and codes without Markdown stripping."""

    @channel_logfire.instrument("napcat.oauth.send", extract_args=False)
    async def send(
        self, address: ChannelAddress, event: AuthorizationEvent
    ) -> IMMessageID | None:
        if isinstance(event, LinkProfileAuthorizationEvent):
            body = (
                f"{event.host} authorization\n{link_profile_body(event)}\n\n"
                f"{event.authorization.authorization_uri}"
            )
        elif isinstance(event, OAuthDeviceAuthorizationEvent):
            body = (
                f"Connect {event.label}\n{event.authorization_uri}\n\n"
                f"Code: {event.user_code}\n\n"
                "Enter the code, then return here and tell me to confirm."
            )
        else:
            body = (
                f"Connect {event.label}\n{event.authorization_uri}\n\n"
                "Open the link and approve the request."
            )
        chat_id = address.chat_id or address.user_id
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            [
                NapcatOutboundMessage(
                    segments=[{"type": "text", "data": {"text": body}}]
                )
            ],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
