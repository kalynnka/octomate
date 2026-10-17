"""Slack artifact overviews, with a link to the authenticated viewer."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from octomate.schemas.artifacts import McpAppOverview
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import slack_logfire
from octomate.tentacles.channel import Ink
from octomate.tentacles.feelers.artifacts import ArtifactFeeler
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.schema import SlackOutboundMessage

if TYPE_CHECKING:
    from octomate.managers.artifacts import ArtifactManager
    from octomate.managers.user import UserManager


class SlackArtifactFeeler(ArtifactFeeler):
    """Show a native card with a sparkle icon, saved title/provider, and Open button.

    Title and subtitle are bounded by Slack's 150-character card limits. No app
    resource is fetched; the viewer still checks ownership when the link opens.
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactManager,
        users: UserManager,
        ink: Ink[SlackOutboundMessage],
    ) -> None:
        super().__init__(artifacts=artifacts, users=users)
        self.ink: Ink[SlackOutboundMessage] = ink

    @slack_logfire.instrument("slack.artifact.send", extract_args=False)
    async def send(
        self, address: ChannelAddress, overview: McpAppOverview
    ) -> IMMessageID | None:
        title = overview.title[:150]
        provider = f"{overview.provider_name} · Interactive artifact"[:150]
        text = (
            f"{escape(title, quote=False)}\n{escape(provider, quote=False)}"
            f"\n{overview.viewer_url}"
        )
        return await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    blocks=[
                        {
                            "type": "card",
                            "slack_icon": {"type": "icon", "name": "sparkle"},
                            "title": {"type": "plain_text", "text": title},
                            "subtitle": {"type": "plain_text", "text": provider},
                            "actions": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Open"},
                                    "accessibility_label": "Open artifact in browser",
                                    "url": str(overview.viewer_url),
                                    "action_id": SlackBlockAction.ARTIFACT_OPEN.value,
                                },
                            ],
                        },
                    ],
                )
            ],
            channel_thread_id=(
                address.channel_thread_id or address.chat_id or address.user_id
            ),
        )
