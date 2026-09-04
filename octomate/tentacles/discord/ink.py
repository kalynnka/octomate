from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeAlias, TypedDict
from urllib.parse import urlparse

import discord
import httpx

from octomate.schemas.segments import ImageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.output import IMMessageID
from octomate.utils import strip_markdown

DiscordMessageable: TypeAlias = discord.DMChannel | discord.TextChannel | discord.Thread


class DiscordSendKwargs(TypedDict, total=False):
    files: list[discord.File]
    allowed_mentions: discord.AllowedMentions
    reference: discord.PartialMessage
    mention_author: bool


class DiscordInk(Ink[DiscordOutboundMessage]):
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
        destination = await self.resolve_messageable(channel_id)
        async with destination.typing():
            yield

    async def start_public_thread(self, chat_id: str, hint_text: str) -> str:
        destination = await self.resolve_messageable(chat_id)
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
