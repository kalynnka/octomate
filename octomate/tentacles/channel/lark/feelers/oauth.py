"""Lark's card for a pending OAuth authorization.

The card carries the whole errand: a link button that opens the provider's device
page, the one-time code to paste there, and a confirm button that finishes the
connection from the card itself — the user never has to come back and say so. The
card rewrites in place with the outcome; `LarkTentacle.on_card_action` owns that
half, since only the tentacle holds the OAuth manager.

The confirm button carries the authorization back with it, so a press that lands
before the provider has accepted the code can redraw the same card — buttons and
all — with a note, rather than replacing it with a dead end.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import lark_logfire
from octomate.tentacles.channel.feelers.oauth import OAuthFeeler
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.lark.feelers import cards
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.tentacles.channel.lark.ink import LarkInk


class LarkOAuthFeeler(OAuthFeeler):
    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    @lark_logfire.instrument("lark.oauth.present", extract_args=False)
    async def present(
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


def authorization_card_data(
    event: OAuthAuthorizationEvent,
    *,
    note: str | None = None,
) -> JsonObject:
    # Lark's card markdown has no code span — backticks would render literally —
    # so the code leans on bold to stand apart from the sentence around it.
    body = [
        cards.markdown(
            f"**{event.user_code}**\n\n"
            f"Enter this code on {event.label} to link your account, then come "
            "back here and finish up."
        )
    ]
    if note is not None:
        body.append(cards.markdown(note))
    body += [
        cards.divider(),
        cards.action(
            [
                cards.button(
                    f"Open {event.label} verification page",
                    button_type="primary",
                    action_type="link",
                    url=event.verification_uri,
                ),
                cards.button(
                    "Finish connecting",
                    value={
                        "action": LarkCardAction.OAUTH_CONFIRM.value,
                        "connector_id": event.connector_id,
                        "label": event.label,
                        "verification_uri": event.verification_uri,
                        "user_code": event.user_code,
                    },
                ),
            ]
        ),
    ]
    return cards.simple_card(
        body,
        header=cards.header(f"{event.label} Device OAuth", template="blue"),
    )


def authorization_connected_card_data(
    *,
    label: str,
    account_label: str,
) -> JsonObject:
    return cards.simple_card(
        [cards.markdown(f"{label} connected as **@{account_label}**.")],
        header=cards.header(f"{label} connected", template="green"),
    )


def authorization_failed_card_data(*, label: str, detail: str) -> JsonObject:
    return cards.simple_card(
        [cards.markdown(f"Could not connect {label}: {detail}")],
        header=cards.header(f"{label} Device OAuth", template="red"),
    )
