"""Presenting a pending OAuth authorization, and the plain-text fallback.

An authorization is a two-step errand for the user: open the provider's page with
a one-time code, then tell the agent so it can finish the connection. The event
reaches this feeler as the authorization itself rather than as rendered text, so
a channel with interactive cards can put the link on a button; channels without
cards fall back to the markdown feeler.

Where it lands is this layer's decision, not each channel's — see `deliver_to`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING, Generic, TypeVar

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.channel.feelers.output import IMMessageID, MarkdownFeeler

if TYPE_CHECKING:
    from octomate.tentacles.channel.base import Ink

MessageT = TypeVar("MessageT")


class OAuthFeeler(ABC, Generic[MessageT]):
    """Presents one pending authorization for one response target."""

    def __init__(self, ink: Ink[MessageT]) -> None:
        self.ink = ink

    async def deliver_to(self, address: ChannelAddress) -> ChannelAddress:
        """Where this authorization has to land.

        The code authorizes one person's account, so a request made from a group
        must not be read out to the group: it goes to that person's direct
        messages instead. A platform with nowhere private to reach them gets an
        error rather than the group, which is the whole point of asking.
        """
        if not address.is_group:
            return address
        chat_id = await self.ink.open_dm(address.user_id)
        if chat_id is None:
            raise RuntimeError(
                f"no direct message to reach {address.user_id} on "
                f"{address.channel_tentacle_id}; a one-time code is not going to a group"
            )
        return replace(address, chat_type="private", chat_id=chat_id, thread_id="")

    @abstractmethod
    async def present(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None: ...


class PlainTextOAuthFeeler(OAuthFeeler[MessageT]):
    def __init__(self, ink: Ink[MessageT], markdown: MarkdownFeeler) -> None:
        super().__init__(ink)
        self.markdown = markdown

    @channel_logfire.instrument("plaintext.oauth.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None:
        return await self.markdown.present(
            await self.deliver_to(address),
            (
                f"[Connect {event.label}]({event.verification_uri})\n\n"
                f"Code: `{event.user_code}`\n\n"
                f"After {event.label} accepts the code, return here and tell me "
                "to confirm."
            ),
        )
