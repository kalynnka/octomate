"""Slack's blocks for a pending OAuth authorization.

Same errand as every channel's card: open the provider's device page with the
one-time code. Finishing is the agent's, not the message's — the user says so in
chat and the capability's confirm tool completes the connection — so these blocks
carry no state back and never redraw.
"""

from __future__ import annotations

from octomate.capabilities.harness.events import (
    LinkProfileAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import slack_logfire
from octomate.tentacles.feelers.oauth import (
    AuthorizationEvent,
    OAuthFeeler,
    link_profile_body,
)
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
        event: AuthorizationEvent,
    ) -> IMMessageID | None:
        text = (
            f"{event.host} authorization"
            if isinstance(event, LinkProfileAuthorizationEvent)
            else f"Connect {event.label}"
        )
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
            channel_thread_id=(
                address.channel_thread_id or address.chat_id or address.user_id
            ),
        )


def authorization_blocks(event: AuthorizationEvent) -> list[SlackBlock]:
    code_blocks: list[SlackBlock] = []
    if isinstance(event, LinkProfileAuthorizationEvent):
        body = f"*{event.host} authorization*\n{link_profile_body(event)}"
        button_label = f"Continue in {event.host}"
        authorization_uri = str(event.authorization.authorization_uri)
    elif isinstance(event, OAuthDeviceAuthorizationEvent):
        body = (
            f"*Connect {event.label}*\nEnter this code on {event.label} to link your "
            f"account, then tell me here and I will finish the connection."
        )
        code_blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"`{event.user_code}`"},
            }
        ]
        button_label = f"Open {event.label} verification page"
        authorization_uri = event.authorization_uri
    else:
        body = (
            f"*Connect {event.label}*\nOpen {event.label} and approve the request to "
            "link your account. Approving is the whole of it."
        )
        button_label = f"Open {event.label} verification page"
        authorization_uri = event.authorization_uri
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
                        "text": button_label,
                    },
                    "style": "primary",
                    "url": authorization_uri,
                    "action_id": SlackBlockAction.OAUTH_OPEN.value,
                }
            ],
        },
    ]
