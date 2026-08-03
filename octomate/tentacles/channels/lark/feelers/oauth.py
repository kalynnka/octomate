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
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import lark_logfire
from octomate.tentacles.channels.feelers.oauth import OAuthFeeler
from octomate.tentacles.channels.feelers.output import IMMessageID
from octomate.tentacles.channels.lark.feelers import cards
from octomate.tentacles.channels.lark.schema import LarkOutboundMessage
from octomate.types.json import JsonObject


class LarkOAuthFeeler(OAuthFeeler[LarkOutboundMessage]):
    @lark_logfire.instrument("lark.oauth.send", extract_args=False)
    async def send(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None:
        reply_to = address.thread_id if address.thread_id.startswith("om_") else None
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
            reply_to,
            reply_in_thread=reply_to is not None,
        )


def authorization_card_data(event: OAuthAuthorizationEvent) -> JsonObject:
    if isinstance(event, OAuthDeviceAuthorizationEvent):
        # Lark's card markdown has no code span — backticks would render literally —
        # so the code leans on bold to stand apart from the sentence around it.
        body = (
            f"**{event.user_code}**\n\n"
            f"Enter this code on {event.label} to link your account, then tell "
            "me here and I will finish the connection."
        )
        title = f"{event.label} Device OAuth"
    else:
        body = (
            f"Open {event.label} and approve the request to link your account. "
            "Approving is the whole of it — nothing to come back and type."
        )
        title = f"{event.label} OAuth"
    return cards.simple_card(
        [
            cards.markdown(body),
            cards.divider(),
            cards.action(
                [
                    cards.button(
                        f"Open {event.label} verification page",
                        button_type="primary",
                        action_type="link",
                        url=event.authorization_uri,
                    )
                ]
            ),
        ],
        header=cards.header(title, template="blue"),
    )
