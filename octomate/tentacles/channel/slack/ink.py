from __future__ import annotations

import logging
import textwrap
from typing import Any

import httpx
from pydantic import SecretStr
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.client import WebClient

from octomate.schemas.segments import ImageSegment
from octomate.tentacles.channel.base import DownloadedImage
from octomate.tentacles.channel.slack.schema import (
    SlackOutboundMessage,
    SlackUserProfile,
)

logger = logging.getLogger(__name__)

BLOCK_TEXT_LIMIT = 3000
MAX_BLOCKS = 50


def _split_oversized_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in blocks:
        text_obj = block.get("text") if block.get("type") == "section" else None
        if isinstance(text_obj, dict):
            content = text_obj.get("text", "")
            if len(content) > BLOCK_TEXT_LIMIT:
                text_type = text_obj.get("type", "mrkdwn")
                result.extend(
                    {
                        "type": "section",
                        "text": {"type": text_type, "text": chunk},
                    }
                    for chunk in textwrap.wrap(content, BLOCK_TEXT_LIMIT)
                )
                continue
        result.append(block)
    return result


class SlackInk:
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
        self,
        resource_id: str,
        **kwargs: Any,
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
                return resp.content, filename
        except Exception:
            logger.warning("SlackInk: download_media failed", exc_info=True)
            return None

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        resource_id = seg.data.url or seg.data.file
        if not resource_id:
            return None
        result = await self.download_media(resource_id)
        if result is None:
            return None
        data, file_name = result
        return DownloadedImage(data=data, file_name=file_name)

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        first_msg_id: str | None = None
        thread_ts = reply_to
        for msg in messages:
            try:
                blocks = msg.blocks
                all_blocks = _split_oversized_blocks(blocks) if blocks else None
                batches = (
                    [
                        all_blocks[i : i + MAX_BLOCKS]
                        for i in range(0, len(all_blocks), MAX_BLOCKS)
                    ]
                    if all_blocks
                    else [None]
                )
                for batch in batches:
                    kwargs: dict[str, Any] = {"channel": chat_id, "text": msg.text}
                    if batch:
                        kwargs["blocks"] = batch
                    if thread_ts:
                        kwargs["thread_ts"] = thread_ts
                    resp = await self.client.chat_postMessage(**kwargs)
                    first_msg_id = first_msg_id or resp.get("ts")
            except Exception:
                logger.warning("SlackInk: send_message failed", exc_info=True)
            if not reply_in_thread:
                thread_ts = None
        return first_msg_id

    async def update_message(
        self,
        channel: str,
        message_id: str,
        text: str = "",
        blocks: list[dict[str, Any]] | None = None,
    ) -> bool:
        try:
            kwargs: dict[str, Any] = {
                "channel": channel,
                "ts": message_id,
                "text": text,
            }
            if blocks:
                kwargs["blocks"] = _split_oversized_blocks(blocks)[:MAX_BLOCKS]
            await self.client.chat_update(**kwargs)
            return True
        except Exception:
            logger.warning("SlackInk: update_message failed", exc_info=True)
            return False

    async def set_assistant_status(
        self,
        channel: str,
        thread_ts: str,
        status: str,
    ) -> None:
        try:
            await self.client.api_call(
                "assistant.threads.setStatus",
                params={
                    "channel_id": channel,
                    "thread_ts": thread_ts,
                    "status": status,
                },
            )
        except Exception:
            logger.debug("SlackInk: set_assistant_status failed", exc_info=True)
