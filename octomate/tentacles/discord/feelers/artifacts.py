"""Discord artifact cards linking to the authenticated web viewer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from octomate.schemas.artifacts import McpAppOverview
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.channel import Ink
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.artifacts import ArtifactFeeler
from octomate.tentacles.feelers.output import IMMessageID

if TYPE_CHECKING:
    from octomate.managers.artifacts import ArtifactManager
    from octomate.managers.user import UserManager


class DiscordArtifactFeeler(ArtifactFeeler):
    """A Components V2 container with a title, provider, and viewer link button.

    Saved metadata is bounded and escaped for Discord's Markdown text displays.
    The link opens the viewer directly without an interaction callback.
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactManager,
        users: UserManager,
        ink: Ink[DiscordOutboundMessage],
    ) -> None:
        super().__init__(artifacts=artifacts, users=users)
        self.ink: Ink[DiscordOutboundMessage] = ink

    @channel_logfire.instrument("discord.artifact.send", extract_args=False)
    async def send(
        self, address: ChannelAddress, overview: McpAppOverview
    ) -> IMMessageID | None:
        title = discord.utils.escape_markdown(
            discord.utils.escape_mentions(" ".join(overview.title.split())[:256]),
            ignore_links=False,
        )
        provider = discord.utils.escape_markdown(
            discord.utils.escape_mentions(
                " ".join(overview.provider_name.split())[:256]
            ),
            ignore_links=False,
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"### ✨ {title}"),
                    discord.ui.TextDisplay(f"-# {provider} · Interactive artifact"),
                    accessory=discord.ui.Button(
                        label="Open artifact",
                        style=discord.ButtonStyle.link,
                        url=str(overview.viewer_url),
                    ),
                ),
                accent_colour=discord.Colour.blurple(),
            )
        )
        chat_id = address.chat_id or address.user_id
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            [DiscordOutboundMessage(view=view)],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
