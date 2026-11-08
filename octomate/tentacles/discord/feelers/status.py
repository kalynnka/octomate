"""Discord activity as one editable message and a bounded typing lifetime."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import discord

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.status import StatusFeeler, StatusUpdate


class DiscordStatusFeeler(StatusFeeler):
    update_interval: ClassVar[float] = 1.0
    refresh_interval: ClassVar[float] = float("inf")

    def __init__(self, ink: DiscordInk) -> None:
        super().__init__()
        self.ink = ink
        self.messages: dict[ThreadKey, str] = {}

    async def render(
        self,
        key: ThreadKey,
        address: ChannelAddress,
        queue: asyncio.Queue[StatusUpdate],
    ) -> None:
        chat_id = address.chat_id or address.user_id
        destination = (
            address.channel_thread_id if address.chat_type == "thread" else chat_id
        )
        async with self.ink.typing(destination or chat_id):
            await super().render(key, address, queue)

    async def present(self, address: ChannelAddress, text: str) -> None:
        key = ThreadKey.from_address(address)
        chat_id = address.chat_id or address.user_id
        destination = (
            address.channel_thread_id if address.chat_type == "thread" else chat_id
        )
        message_id = self.messages.get(key)
        if not text:
            if message_id is not None:
                self.messages.pop(key)
                await self.ink.delete_message(destination or chat_id, message_id)
            return
        content = discord.utils.escape_markdown(discord.utils.escape_mentions(text))
        if message_id is not None:
            await self.ink.edit_message(destination or chat_id, message_id, content)
            return
        sent = await self.ink.send_message(
            chat_id,
            address.chat_type,
            [DiscordOutboundMessage(content=content)],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
        if sent is not None:
            self.messages[key] = sent
