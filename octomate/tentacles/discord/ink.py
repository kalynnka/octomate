from __future__ import annotations

import discord

from octomate.schemas.segments import ImageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.output import IMMessageID


class DiscordInk(Ink[DiscordOutboundMessage]):
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    async def inspect(self) -> UserProfile:
        user = self.client.user
        if user is None:
            raise RuntimeError("DiscordInk: client is not logged in")
        return UserProfile(channel_user_id=str(user.id), name=user.display_name)

    async def get_user_profile(self, user_id: str) -> UserProfile:
        snowflake = int(user_id)
        user = self.client.get_user(snowflake)
        if user is None:
            user = await self.client.fetch_user(snowflake)
        return UserProfile(channel_user_id=str(user.id), name=user.display_name)

    async def upload_media(self, data: bytes) -> str | None:
        raise NotImplementedError(
            "DiscordInk: media upload requires the message sending implementation"
        )

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        raise NotImplementedError("DiscordInk: image download is not implemented")

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[DiscordOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        raise NotImplementedError("DiscordInk: message sending is not implemented")
