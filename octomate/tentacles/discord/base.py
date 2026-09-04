from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Self

import discord
from pydantic import SecretStr

from octomate.config import DiscordChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.channel import (
    ChannelSurfaces,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.discord.chromo import DiscordChromo
from octomate.tentacles.discord.feelers.output import DiscordTimelineFeeler
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class DiscordTentacle(ChannelTentacle[discord.Message, DiscordOutboundMessage]):
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(
        sub_thread=True, direct_message=True
    )
    client: discord.Client
    ink: DiscordInk
    chromo: DiscordChromo
    bot_token: SecretStr
    gateway_task: asyncio.Task[None] | None
    ingest_tasks: set[asyncio.Task[None]]

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
        self.ingest_tasks = set()
        self.feelers.timeline = DiscordTimelineFeeler(
            ink=self.ink,
            chromo=self.chromo,
            stream_config=config.stream,
            ask_questions=self.feelers.ask_questions,
            approvals=self.feelers.approvals,
            oauth=self.feelers.oauth,
            deferred_actions=self.octomate.deferred_actions,
        )
        self.client.event(self.on_message)

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

    async def on_message(self, message: discord.Message) -> None:
        client_user = self.client.user
        if (
            message.author.bot
            or message.webhook_id is not None
            or message.is_system()
            or (client_user is not None and message.author.id == client_user.id)
        ):
            return

        task = asyncio.create_task(
            self.ingest(message),
            name=f"discord:{self.id}:message:{message.id}",
        )
        self.ingest_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self.ingest_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error(
                    "Channel %s: failed to handle Discord message",
                    self.id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finish)

    async def start_sub_thread(
        self,
        address: ChannelAddress,
        hint_text: str,
    ) -> ChannelAddress:
        if address.chat_type != "group":
            return await super().start_sub_thread(address, hint_text)
        thread_id = await self.ink.start_public_thread(
            address.chat_id or address.user_id,
            hint_text,
        )
        return replace(address, chat_type="thread", channel_thread_id=thread_id)
