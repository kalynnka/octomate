"""Presenting a pending OAuth authorization, and the plain-text fallback.

An authorization is a two-step errand for the user: open the provider's page with
a one-time code, then come back. A channel with interactive cards can carry both
steps — the link, and a button that finishes the connection without the user
having to say anything — so the event reaches this feeler as the authorization
itself rather than as a rendered message. Channels without cards fall back to the
markdown feeler, where the second step is the user telling the agent to confirm.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.channel.feelers.output import IMMessageID, MarkdownFeeler


class OAuthFeeler(ABC):
    """Presents one pending authorization for one response target."""

    @abstractmethod
    async def present(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None: ...


class PlainTextOAuthFeeler(OAuthFeeler):
    def __init__(self, markdown: MarkdownFeeler) -> None:
        self.markdown = markdown

    @channel_logfire.instrument("plaintext.oauth.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None:
        return await self.markdown.present(
            address,
            (
                f"[Connect {event.label}]({event.verification_uri})\n\n"
                f"Code: `{event.user_code}`\n\n"
                f"After {event.label} accepts the code, return here and tell me "
                "to confirm."
            ),
        )
