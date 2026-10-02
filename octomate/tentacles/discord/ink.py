"""Discord transport over discord.py: sending, editing, typing and thread creation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import discord
import httpx

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import ImageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import (
    DownloadedImage,
    Ink,
)
from octomate.tentacles.discord.schema import (
    DiscordAddress,
    DiscordAddressMetadata,
    DiscordOutboundMessage,
)
from octomate.tentacles.feelers.output import IMMessageID
from octomate.utils import strip_markdown

type DiscordMessageable = discord.DMChannel | discord.TextChannel | discord.Thread

logger = logging.getLogger(__name__)


class DiscordSendKwargs(TypedDict, total=False):
    """The keyword arguments `send_message` passes to `Messageable.send`."""

    files: list[discord.File]
    allowed_mentions: discord.AllowedMentions
    reference: discord.PartialMessage
    mention_author: bool


class DiscordInk(Ink[DiscordOutboundMessage]):
    """Discord transport over a logged-in discord.py client; edits a message in
    place for streaming and opens a public thread for a sub-thread."""

    def __init__(
        self,
        client: discord.Client,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.client = client
        self.http = http or httpx.AsyncClient(follow_redirects=True)

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    async def inspect(self) -> UserProfile:
        user = self.client.user
        if user is None:
            raise RuntimeError("DiscordInk: client is not logged in")
        return UserProfile(channel_user_id=str(user.id), name=user.display_name)

    async def get_user_profile(self, user_id: str) -> UserProfile:
        user = await self.resolve_user(user_id)
        return UserProfile(channel_user_id=str(user.id), name=user.display_name)

    async def resolve_user(self, user_id: str) -> discord.User:
        snowflake = int(user_id)
        user = self.client.get_user(snowflake)
        if user is None:
            user = await self.client.fetch_user(snowflake)
        return user

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        resource = seg.data.url or seg.data.file
        if not resource:
            return None
        response = await self.http.get(resource)
        response.raise_for_status()
        file_name = (
            seg.data.name or Path(urlparse(resource).path).name or f"{message_id}.png"
        )
        return DownloadedImage(
            data=response.content,
            file_name=file_name,
            content_type=response.headers.get("content-type", ""),
            url=resource,
        )

    async def resolve_messageable(self, channel_id: str) -> DiscordMessageable:
        snowflake = int(channel_id)
        channel = self.client.get_channel(snowflake)
        if channel is None:
            channel = await self.client.fetch_channel(snowflake)
        if not isinstance(
            channel,
            (discord.DMChannel, discord.TextChannel, discord.Thread),
        ):
            raise TypeError(f"DiscordInk: channel {channel_id} cannot carry messages")
        return channel

    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None:
        if not user_id:
            return None
        user = await self.resolve_user(user_id)
        channel = await self.client.create_dm(user)
        return str(channel.id)

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[DiscordOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        if not messages:
            return None
        destination_id = channel_thread_id if chat_type == "thread" else chat_id
        destination = await self.resolve_messageable(destination_id)
        reference = destination.get_partial_message(int(reply_to)) if reply_to else None
        first_message_id: IMMessageID | None = None

        for index, message in enumerate(messages):
            files: list[discord.File] = []
            try:
                for path in message.attachment_paths:
                    files.append(discord.File(path))
                payload = DiscordSendKwargs(
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=[
                            discord.Object(id=int(user_id))
                            for user_id in message.mentioned_user_ids
                        ],
                        roles=False,
                        replied_user=False,
                    ),
                    mention_author=False,
                )
                if files:
                    payload["files"] = files
                if index == 0 and reference is not None:
                    payload["reference"] = reference
                if isinstance(message.view, discord.ui.LayoutView):
                    sent = await destination.send(view=message.view, **payload)
                elif message.view is not None:
                    sent = await destination.send(
                        message.content or None,
                        view=message.view,
                        **payload,
                    )
                else:
                    sent = await destination.send(message.content or None, **payload)
            finally:
                for file in files:
                    file.close()
            first_message_id = first_message_id or str(sent.id)
        return first_message_id

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        mentioned_user_ids: tuple[str, ...] = (),
    ) -> IMMessageID:
        destination = await self.resolve_messageable(channel_id)
        message = await destination.get_partial_message(int(message_id)).edit(
            content=content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=[
                    discord.Object(id=int(user_id)) for user_id in mentioned_user_ids
                ],
                roles=False,
                replied_user=False,
            ),
        )
        return str(message.id)

    @asynccontextmanager
    async def typing(self, channel_id: str) -> AsyncGenerator[None]:
        async def keep_typing() -> None:
            destination = await self.resolve_messageable(channel_id)
            async with destination.typing():
                await asyncio.Future[None]()

        # Typing is a UI hint; neither its channel lookup nor its first HTTP request
        # should stand between an incoming message and starting the agent.
        task = asyncio.create_task(keep_typing())
        try:
            yield
        finally:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            except Exception:
                logger.warning(
                    "Discord typing hint failed for %s", channel_id, exc_info=True
                )

    async def suggest_addresses(
        self, address: ChannelAddress, source_address: ChannelAddress | None = None
    ) -> list[ChannelAddress]:
        """Eligible text channels in the current conversation's server only."""
        if (
            source_address is None
            or source_address.channel_thread_id
            or source_address.chat_type != "group"
        ):
            return []
        parent = await self.resolve_messageable(source_address.chat_id)
        if not isinstance(parent, discord.TextChannel):
            return []
        guild = parent.guild
        member = guild.get_member(int(address.user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(address.user_id))
            except discord.NotFound:
                return []
        return [
            ChannelAddress(
                channel_tentacle_id=address.channel_tentacle_id,
                user_id=address.user_id,
                chat_type="group",
                chat_id=str(channel.id),
                shared=True,
            )
            for channel in guild.text_channels
            if self.thread_permissions(channel, member)
        ]

    async def list_addresses(
        self, address: ChannelAddress, inside: str | None = None
    ) -> list[ChannelAddress]:
        """The servers the requester shares with the bot, then the channels the
        requester sees in one, barred where a thread cannot start. Listing servers
        looks membership up in each."""
        user_id = int(address.user_id)
        if inside is None:
            guilds = self.client.guilds
            members = await asyncio.gather(
                *(self.member(guild, user_id) for guild in guilds)
            )
            return [
                DiscordAddress(
                    channel_tentacle_id=address.channel_tentacle_id,
                    user_id=address.user_id,
                    chat_type="group",
                    chat_id="",
                    shared=True,
                    metadata={"name": guild.name, "inside": str(guild.id)},
                )
                for guild, member in zip(guilds, members, strict=True)
                if member is not None
            ]
        guild = self.client.get_guild(int(inside)) if inside.isdecimal() else None
        member = await self.member(guild, user_id) if guild is not None else None
        if guild is None or member is None:
            raise ValueError("You and the bot do not share that Discord server.")
        listed: list[ChannelAddress] = []
        for channel in guild.text_channels:
            user = channel.permissions_for(member)
            if not user.view_channel:
                continue
            metadata: DiscordAddressMetadata = {
                "name": channel.name,
                "server": guild.name,
            }
            if channel.type is not discord.ChannelType.text:
                metadata["barred"] = "A thread starts only in a text channel."
            elif not user.send_messages_in_threads:
                metadata["barred"] = "You cannot post in threads here."
            elif not channel.permissions_for(guild.me).view_channel:
                metadata["barred"] = "The bot cannot see this channel."
            elif not self.thread_permissions(channel, member):
                metadata["barred"] = "The bot cannot start a thread here."
            listed.append(
                DiscordAddress(
                    channel_tentacle_id=address.channel_tentacle_id,
                    user_id=address.user_id,
                    chat_type="group",
                    chat_id=str(channel.id),
                    shared=True,
                    metadata=metadata,
                )
            )
        return listed

    async def member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """The requester's membership, cached or looked up once; None for a stranger."""
        member = guild.get_member(user_id)
        with suppress(discord.NotFound):
            member = member or await guild.fetch_member(user_id)
        return member

    def thread_permissions(
        self, channel: discord.TextChannel, member: discord.Member
    ) -> bool:
        """Both the requester and bot must be able to participate in the new thread."""
        user = channel.permissions_for(member)
        bot = channel.permissions_for(channel.guild.me)
        return (
            channel.type is discord.ChannelType.text
            and user.view_channel
            and user.send_messages_in_threads
            and bot.view_channel
            and bot.send_messages
            and bot.create_public_threads
            and bot.send_messages_in_threads
        )

    async def prepare_address(
        self, address: ChannelAddress, source_address: ChannelAddress | None = None
    ) -> ChannelAddress:
        if (
            address.chat_type != "group"
            or not address.chat_id
            or address.channel_thread_id
        ):
            raise ValueError("Discord requires a server text channel as parent.")
        try:
            destination = await self.resolve_messageable(address.chat_id)
            if not isinstance(destination, discord.TextChannel):
                raise ValueError("Discord requires a server text channel as parent.")
            member = await destination.guild.fetch_member(int(address.user_id))
        except (discord.HTTPException, TypeError) as error:
            raise ValueError(
                "The Discord parent or membership is inaccessible."
            ) from error
        if not self.thread_permissions(destination, member):
            raise ValueError("The requester or bot cannot use this Discord parent.")
        return replace(address, shared=True)

    async def start_public_thread(
        self, chat_id: str, hint_text: str, *, user_id: str | None = None
    ) -> str:
        """Create a public thread; recheck a supplied requester's membership and access."""
        destination = await self.resolve_messageable(chat_id)
        if user_id is not None:
            if not isinstance(destination, discord.TextChannel):
                raise ValueError(
                    "The Discord destination is not a server text channel."
                )
            member = await destination.guild.fetch_member(int(user_id))
            if not self.thread_permissions(destination, member):
                raise ValueError(
                    "The requester or bot can no longer use this Discord channel."
                )
        if (
            not isinstance(destination, discord.TextChannel)
            or destination.type is not discord.ChannelType.text
        ):
            raise TypeError(
                f"DiscordInk: channel {chat_id} cannot start a public thread"
            )
        opener = await destination.send(
            content=hint_text,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=[],
                roles=False,
                replied_user=False,
            ),
            mention_author=False,
        )
        thread_name = " ".join(strip_markdown(hint_text).split())[:100]
        thread = await opener.create_thread(name=thread_name or "Octomate thread")
        return str(thread.id)
