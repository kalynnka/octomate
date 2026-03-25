from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import Field, SecretStr
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.client import WebClient

from octomate.schemas.session import UserProfile

logger = logging.getLogger(__name__)


class SlackUserProfile(UserProfile):
    user_id: str = ""
    name: str = ""
    nickname: str | None = Field(default=None)
    title: str | None = None


class SlackInk:
    bot_token: SecretStr
    client: AsyncWebClient
    sync_client: WebClient

    def __init__(self, bot_token: SecretStr) -> None:
        self.bot_token = bot_token
        token = bot_token.get_secret_value()
        self.client = AsyncWebClient(token=token)
        self.sync_client = WebClient(token=token)

    def inspect(self) -> SlackUserProfile:
        resp = self.sync_client.auth_test()
        bot_user_id = resp.get("user_id", "")
        user_resp = self.sync_client.users_info(user=bot_user_id)
        user = user_resp.get("user", {})
        profile = user.get("profile", {})
        return SlackUserProfile(
            user_id=bot_user_id,
            name=profile.get("real_name") or user.get("real_name", bot_user_id),
            nickname=profile.get("display_name") or None,
            title=profile.get("title") or None,
        )

    async def get_user_profile(self, user_id: str) -> SlackUserProfile:
        try:
            resp = await self.client.users_info(user=user_id)
            user = resp.get("user", {})
            profile = user.get("profile", {})
            return SlackUserProfile(
                user_id=user_id,
                name=profile.get("real_name") or user.get("real_name", user_id),
                nickname=profile.get("display_name") or None,
                title=profile.get("title") or None,
            )
        except Exception:
            logger.warning(
                "SlackInk: get_user_profile failed for %s", user_id, exc_info=True
            )
            return SlackUserProfile(user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        try:
            resp = await self.client.files_upload_v2(content=data, filename="image.png")
            file_info = resp.get("file", {})
            return file_info.get("permalink") or file_info.get("url_private")
        except Exception:
            logger.warning("SlackInk: upload_media failed", exc_info=True)
            return None

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    resource_id,
                    headers={
                        "Authorization": f"Bearer {self.bot_token.get_secret_value()}"
                    },
                    follow_redirects=True,
                )
                resp.raise_for_status()
                filename = resource_id.rsplit("/", 1)[-1] or "file"
                return (resp.content, filename)
        except Exception:
            logger.warning("SlackInk: download_media failed", exc_info=True)
            return None

    async def send_message(
        self,
        channel: str,
        text: str = "",
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        try:
            kwargs: dict[str, Any] = {"channel": channel, "text": text}
            if blocks:
                kwargs["blocks"] = blocks
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            resp = await self.client.chat_postMessage(**kwargs)
            return resp.get("ts")
        except Exception:
            logger.warning("SlackInk: send_message failed", exc_info=True)
            return None

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str = "",
        blocks: list[dict] | None = None,
    ) -> bool:
        try:
            kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
            if blocks:
                kwargs["blocks"] = blocks
            await self.client.chat_update(**kwargs)
            return True
        except Exception:
            logger.warning("SlackInk: update_message failed", exc_info=True)
            return False

    async def set_assistant_status(
        self, channel: str, thread_ts: str, status: str
    ) -> None:
        try:
            await self.client.api_call(
                "assistant.threads.setStatus",
                params={"channel_id": channel, "thread_ts": thread_ts, "status": status},
            )
        except Exception:
            logger.debug("SlackInk: set_assistant_status failed", exc_info=True)

    async def set_suggested_prompts(
        self, channel: str, thread_ts: str, prompts: list[dict]
    ) -> None:
        try:
            await self.client.api_call(
                "assistant.threads.setSuggestedPrompts",
                params={"channel_id": channel, "thread_ts": thread_ts},
                json={"prompts": prompts},
            )
        except Exception:
            logger.debug("SlackInk: set_suggested_prompts failed", exc_info=True)

    async def pin_message(self, channel: str, ts: str) -> bool:
        try:
            await self.client.pins_add(channel=channel, timestamp=ts)
            return True
        except Exception:
            logger.warning("SlackInk: pin_message failed", exc_info=True)
            return False

    async def unpin_message(self, channel: str, ts: str) -> bool:
        try:
            await self.client.pins_remove(channel=channel, timestamp=ts)
            return True
        except Exception:
            logger.warning("SlackInk: unpin_message failed", exc_info=True)
            return False

    async def set_title(self, channel: str, thread_ts: str, title: str) -> None:
        try:
            await self.client.api_call(
                "assistant.threads.setTitle",
                params={"channel_id": channel, "thread_ts": thread_ts, "title": title},
            )
        except Exception:
            logger.debug("SlackInk: set_title failed", exc_info=True)
