from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self

import discord
from pydantic import SecretStr

from octomate.config import DiscordChannelConfig
from octomate.tentacles.channel import ChannelTentacle
from octomate.tentacles.discord.chromo import DiscordChromo
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class DiscordTentacle(ChannelTentacle[discord.Message, DiscordOutboundMessage]):
    client: discord.Client
    ink: DiscordInk
    chromo: DiscordChromo
    bot_token: SecretStr
    gateway_task: asyncio.Task[None] | None

    @property
    def log_names(self) -> tuple[str, ...]:
        return (*super().log_names, "discord")

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: DiscordChannelConfig,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.ink = DiscordInk(self.client)
        self.chromo = DiscordChromo()
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            config=config,
        )
        self.bot_token = config.bot_token
        self.gateway_task = None

    async def __aenter__(self) -> Self:
        await self.client.login(self.bot_token.get_secret_value())
        await super().__aenter__()
        logger.info("Channel %s: connecting Discord Gateway client", self.id)
        self.gateway_task = asyncio.create_task(
            self.client.connect(reconnect=True),
            name=f"discord:{self.id}",
        )
        await self.client.wait_until_ready()
        logger.info("Channel %s: Discord Gateway client is ready", self.id)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.close()
        if self.gateway_task is not None:
            gateway_task = self.gateway_task
            self.gateway_task = None
            try:
                await gateway_task
            except asyncio.CancelledError:
                pass
        await super().__aexit__(*exc)
