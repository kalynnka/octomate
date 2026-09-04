"""Slack's blocks for a pending OAuth authorization.

Same errand as every channel's card: open the provider's device page with the
one-time code. Finishing is the agent's, not the message's — the user says so in
chat and the capability's confirm tool completes the connection — so these blocks
carry no state back and never redraw.
"""

from __future__ import annotations

from octomate.capabilities.harness.events import (
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import slack_logfire
from octomate.tentacles.feelers.oauth import OAuthFeeler
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.schema import (
    SlackBlock,
    SlackOutboundMessage,
)


class SlackOAuthFeeler(OAuthFeeler[SlackOutboundMessage]):
    @slack_logfire.instrument("slack.oauth.send", extract_args=False)
    async def send(
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
            address.channel_thread_id or None,
        )


def authorization_blocks(event: OAuthAuthorizationEvent) -> list[SlackBlock]:
    if isinstance(event, OAuthDeviceAuthorizationEvent):
        body = (
            f"*Connect {event.label}*\nEnter this code on {event.label} to link your "
            f"account, then tell me here and I will finish the connection."
        )
        code_blocks: list[SlackBlock] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"`{event.user_code}`"},
            }
        ]
    else:
        body = (
            f"*Connect {event.label}*\nOpen {event.label} and approve the request to "
            "link your account. Approving is the whole of it."
        )
        code_blocks = []
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        *code_blocks,
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
                    "url": event.authorization_uri,
                    "action_id": SlackBlockAction.OAUTH_OPEN.value,
                }
            ],
        },
    ]
