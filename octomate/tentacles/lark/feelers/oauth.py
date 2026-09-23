"""Lark's card for a pending OAuth authorization.

The card carries the link button that opens the provider's device page and the
one-time code to paste there. Finishing is the agent's errand, not the card's:
the user says so in chat and the capability's confirm tool completes the
connection. A confirm button here would have to come back over Feishu's card
callback, which needs ingress this deployment does not have.
"""

from __future__ import annotations

import json

from octomate.capabilities.harness.events import (
    LinkProfileAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import lark_logfire
from octomate.tentacles.feelers.oauth import (
    AuthorizationEvent,
    OAuthFeeler,
    link_profile_body,
)
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.lark.feelers import cards
from octomate.tentacles.lark.schema import LarkOutboundMessage
from octomate.types.json import JsonObject


class LarkOAuthFeeler(OAuthFeeler[LarkOutboundMessage]):
    """Sends the authorization card: a link button and, for a device flow, the
    one-time code."""

    @lark_logfire.instrument("lark.oauth.send", extract_args=False)
    async def send(
        self,
        address: ChannelAddress,
        event: AuthorizationEvent,
    ) -> IMMessageID | None:
        channel_thread_id = (
            address.channel_thread_id
            if address.channel_thread_id and address.channel_thread_id.startswith("om_")
            else address.chat_id or address.user_id
        )
        return await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=json.dumps(
                        authorization_card_data(event),
                        ensure_ascii=False,
                    ),
                )
            ],
            channel_thread_id=channel_thread_id,
            reply_in_thread=channel_thread_id is not None,
        )


def authorization_card_data(event: AuthorizationEvent) -> JsonObject:
    if isinstance(event, LinkProfileAuthorizationEvent):
        title = f"{event.host} authorization"
        body = link_profile_body(event)
        button_label = f"Continue in {event.host}"
        authorization_uri = str(event.authorization.authorization_uri)
    elif isinstance(event, OAuthDeviceAuthorizationEvent):
        # Lark's card markdown has no code span — backticks would render literally —
        # so the code leans on bold to stand apart from the sentence around it.
        body = (
            f"**{event.user_code}**\n\n"
            f"Enter this code on {event.label} to link your account, then tell "
            "me here and I will finish the connection."
        )
        title = f"{event.label} Device OAuth"
        button_label = f"Open {event.label} verification page"
        authorization_uri = event.authorization_uri
    else:
        body = (
            f"Open {event.label} and approve the request to link your account. "
            "Approving is the whole of it — nothing to come back and type."
        )
        title = f"{event.label} OAuth"
        button_label = f"Open {event.label} verification page"
        authorization_uri = event.authorization_uri
    return cards.simple_card(
        [
            cards.markdown(body),
            cards.divider(),
            cards.action(
                [
                    cards.button(
                        button_label,
                        button_type="primary",
                        action_type="link",
                        url=authorization_uri,
                    )
                ]
            ),
        ],
        header=cards.header(title, template="blue"),
    )
