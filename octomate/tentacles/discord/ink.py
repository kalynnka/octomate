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
# Where a new thread can start: a text channel's thread, or a forum's post.
type DiscordParent = discord.TextChannel | discord.ForumChannel

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

    async def resolve_channel(
        self, channel_id: str
    ) -> discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread:
        """The channel by id, from the client's cache or else the API."""
        snowflake = int(channel_id)
        channel = self.client.get_channel(snowflake)
        if channel is None:
            channel = await self.client.fetch_channel(snowflake)
        return channel

    async def resolve_messageable(self, channel_id: str) -> DiscordMessageable:
        channel = await self.resolve_channel(channel_id)
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
        """Eligible text and forum channels in the current conversation's server
        only. The source is a server channel, so whatever starts there is public."""
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
            for channel in [*guild.text_channels, *guild.forums]
            if self.barred(channel, member, private=False) is None
        ]

    async def list_addresses(
        self,
        address: ChannelAddress,
        inside: str | None = None,
        *,
        private: bool = False,
    ) -> list[ChannelAddress]:
        """The servers the requester shares with the bot, then the text and forum
        channels the requester sees in one, barred where a thread cannot start.
        Listing servers looks membership up in each. A `private` landing in a text
        channel is a private thread; a forum's post is always public."""
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
        for channel in [*guild.text_channels, *guild.forums]:
            if not channel.permissions_for(member).view_channel:
                continue
            metadata: DiscordAddressMetadata = {
                "name": channel.name,
                "server": guild.name,
            }
            if (reason := self.barred(channel, member, private=private)) is not None:
                metadata["barred"] = reason
            listed.append(
                DiscordAddress(
                    channel_tentacle_id=address.channel_tentacle_id,
                    user_id=address.user_id,
                    chat_type="group",
                    chat_id=str(channel.id),
                    shared=not private or isinstance(channel, discord.ForumChannel),
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

    def barred(
        self, channel: DiscordParent, member: discord.Member, *, private: bool
    ) -> str | None:
        """Why the requester and the bot cannot share a new thread in `channel`, or
        None. A `private` thread in a text channel needs the bot to open private
        threads; a forum's post is a message, and public whatever was asked."""
        if channel.type not in {discord.ChannelType.text, discord.ChannelType.forum}:
            return "A thread starts only in a text or forum channel."
        user = channel.permissions_for(member)
        bot = channel.permissions_for(channel.guild.me)
        if not user.view_channel or not user.send_messages_in_threads:
            return "You cannot post in threads here."
        if not bot.view_channel:
            return "The bot cannot see this channel."
        if isinstance(channel, discord.ForumChannel):
            opens = bot.send_messages
        elif private:
            opens = bot.send_messages and bot.create_private_threads
        else:
            opens = bot.send_messages and bot.create_public_threads
        if not opens or not bot.send_messages_in_threads:
            if private and isinstance(channel, discord.TextChannel):
                return "The bot cannot start a private thread here."
            return "The bot cannot start a thread here."
        return None

    async def resolve_parent(self, channel_id: str) -> DiscordParent:
        channel = await self.resolve_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise TypeError(f"DiscordInk: channel {channel_id} cannot hold a thread")
        return channel

    async def prepare_address(
        self,
        address: ChannelAddress,
        source_address: ChannelAddress | None = None,
        *,
        private: bool = False,
    ) -> ChannelAddress:
        if (
            address.chat_type != "group"
            or not address.chat_id
            or address.channel_thread_id
        ):
            raise ValueError(
                "Discord requires a server text or forum channel as parent."
            )
        try:
            destination = await self.resolve_parent(address.chat_id)
            member = await destination.guild.fetch_member(int(address.user_id))
        except (discord.HTTPException, TypeError) as error:
            raise ValueError(
                "The Discord parent or membership is inaccessible."
            ) from error
        if (reason := self.barred(destination, member, private=private)) is not None:
            raise ValueError(reason)
        return replace(
            address,
            shared=not private or isinstance(destination, discord.ForumChannel),
        )

    async def open_thread(
        self,
        chat_id: str,
        hint_text: str,
        *,
        user_id: str | None = None,
        private: bool = False,
    ) -> str:
        """Start a thread with the hint as its first message, and answer its id: a
        public one off a message in a text channel, a `private` one only the
        requester and the bot are in, or a post in a forum. A supplied requester's
        membership and access are checked again."""
        destination = await self.resolve_parent(chat_id)
        if user_id is not None:
            member = await destination.guild.fetch_member(int(user_id))
            if (
                reason := self.barred(destination, member, private=private)
            ) is not None:
                raise ValueError(reason)
        elif private:
            raise ValueError("A private thread needs the requester to add to it.")
        mentions = discord.AllowedMentions(
            everyone=False, users=[], roles=False, replied_user=False
        )
        thread_name = " ".join(strip_markdown(hint_text).split())[:100]
        name = thread_name or "Octomate thread"
        if isinstance(destination, discord.ForumChannel):
            post = await destination.create_thread(
                name=name, content=hint_text, allowed_mentions=mentions
            )
            return str(post.thread.id)
        if private and user_id is not None:
            thread = await destination.create_thread(
                name=name, type=discord.ChannelType.private_thread, invitable=False
            )
            await thread.add_user(discord.Object(id=int(user_id)))
            await thread.send(content=hint_text, allowed_mentions=mentions)
            return str(thread.id)
        opener = await destination.send(
            content=hint_text, allowed_mentions=mentions, mention_author=False
        )
        thread = await opener.create_thread(name=name)
        return str(thread.id)
