from __future__ import annotations

import discord

from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel import Chromo
from octomate.tentacles.discord.schema import DiscordOutboundMessage


class DiscordChromo(Chromo[discord.Message, DiscordOutboundMessage]):
    async def sip(self, raw: discord.Message) -> MessageEvent | None:
        return None

    def outbound_markdown(self, text: str) -> list[DiscordOutboundMessage]:
        return [DiscordOutboundMessage(content=text)] if text else []
