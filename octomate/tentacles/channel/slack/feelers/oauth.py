"""Slack's blocks for a pending OAuth authorization.

Same errand as every channel's card: open the provider's device page with a
one-time code, then confirm. The confirm button finishes the connection from the
message itself — `SlackTentacle.on_oauth_action` owns that half, since only the
tentacle holds the OAuth manager — and carries the authorization with it, so a
press that lands before the provider accepted the code can redraw the same
message rather than replacing it with a dead end.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import slack_logfire
from octomate.tentacles.channel.feelers.oauth import OAuthFeeler
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.schema import (
    SlackBlock,
    SlackOAuthActionValue,
    SlackOutboundMessage,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.slack.ink import SlackInk

SlackOAuthActionValueAdapter = TypeAdapter(SlackOAuthActionValue)


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


def authorization_blocks(
    event: OAuthAuthorizationEvent,
    *,
    note: str | None = None,
) -> list[SlackBlock]:
    value: SlackOAuthActionValue = {
        "connector_id": event.connector_id,
        "label": event.label,
        "verification_uri": event.verification_uri,
        "user_code": event.user_code,
    }
    body = f"*Connect {event.label}*\nOpen {event.label} and enter this code:"
    blocks: list[SlackBlock] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"`{event.user_code}`"}},
    ]
    if note is not None:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"Open {event.label}"},
                    "style": "primary",
                    "url": event.verification_uri,
                    "action_id": SlackBlockAction.OAUTH_OPEN.value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "I've authorized"},
                    "action_id": SlackBlockAction.OAUTH_CONFIRM.value,
                    "value": json.dumps(
                        SlackOAuthActionValueAdapter.dump_python(value, mode="json")
                    ),
                },
            ],
        }
    )
    return blocks


def authorization_connected_blocks(
    *,
    label: str,
    account_label: str,
) -> list[SlackBlock]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{label} connected* as `@{account_label}`.",
            },
        }
    ]


def authorization_failed_blocks(*, label: str, detail: str) -> list[SlackBlock]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Could not connect {label}*\n{detail}",
            },
        }
    ]
