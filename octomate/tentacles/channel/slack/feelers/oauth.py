"""Slack's blocks for a pending OAuth authorization.

Same errand as every channel's card: open the provider's device page with the
one-time code. Finishing is the agent's, not the message's — the user says so in
chat and the capability's confirm tool completes the connection — so these blocks
carry no state back and never redraw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import slack_logfire
from octomate.tentacles.channel.feelers.oauth import OAuthFeeler
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.schema import (
    SlackBlock,
    SlackOutboundMessage,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.slack.ink import SlackInk


class SlackOAuthFeeler(OAuthFeeler):
    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    @slack_logfire.instrument("slack.oauth.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        event: OAuthAuthorizationEvent,
    ) -> IMMessageID | None:
        text = f"Connect {event.label}"
        return await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    markdown_text=text,
                    blocks=authorization_blocks(event),
                )
            ],
            address.thread_id or None,
        )


def authorization_blocks(event: OAuthAuthorizationEvent) -> list[SlackBlock]:
    body = (
        f"*Connect {event.label}*\nEnter this code on {event.label} to link your "
        f"account, then tell me here and I will finish the connection."
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"`{event.user_code}`"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"Open {event.label} verification page",
                    },
                    "style": "primary",
                    "url": event.verification_uri,
                    "action_id": SlackBlockAction.OAUTH_OPEN.value,
                }
            ],
        },
    ]
