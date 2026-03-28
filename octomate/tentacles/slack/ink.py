from __future__ import annotations

import logging
import textwrap
from typing import Any

import httpx
from pydantic import Field, SecretStr
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.client import WebClient

from octomate.schemas.session import UserProfile

logger = logging.getLogger(__name__)

BLOCK_TEXT_LIMIT = 3000
MAX_BLOCKS = 50


def _split_oversized_blocks(blocks: list[dict]) -> list[dict]:
    """Split blocks whose text exceeds Slack's 3000-char limit."""
    result: list[dict] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "section":
            text_obj = block.get("text")
            if text_obj and isinstance(text_obj, dict):
                content = text_obj.get("text", "")
                if len(content) > BLOCK_TEXT_LIMIT:
                    text_type = text_obj.get("type", "mrkdwn")
                    result.extend(
                        {"type": "section", "text": {"type": text_type, "text": c}}
                        for c in textwrap.wrap(content, BLOCK_TEXT_LIMIT)
                    )
                    continue
        result.append(block)
    return result


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
            all_blocks = _split_oversized_blocks(blocks) if blocks else None
            first_ts: str | None = None
            batches = (
                [
                    all_blocks[i : i + MAX_BLOCKS]
                    for i in range(0, len(all_blocks), MAX_BLOCKS)
                ]
                if all_blocks
                else [None]
            )
            for batch in batches:
                kwargs: dict[str, Any] = {"channel": channel, "text": text}
                if batch:
                    kwargs["blocks"] = batch
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                resp = await self.client.chat_postMessage(**kwargs)
                first_ts = first_ts or resp.get("ts")
            return first_ts
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
            all_blocks = _split_oversized_blocks(blocks) if blocks else None
            first_batch = all_blocks[:MAX_BLOCKS] if all_blocks else None
            kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
            if first_batch:
                kwargs["blocks"] = first_batch
            await self.client.chat_update(**kwargs)
            if all_blocks and len(all_blocks) > MAX_BLOCKS:
                for i in range(MAX_BLOCKS, len(all_blocks), MAX_BLOCKS):
                    batch = all_blocks[i : i + MAX_BLOCKS]
                    await self.client.chat_postMessage(
                        channel=channel, text=text, blocks=batch, thread_ts=ts
                    )
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
                params={
                    "channel_id": channel,
                    "thread_ts": thread_ts,
                    "status": status,
                },
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

    async def open_modal(self, trigger_id: str, view: dict) -> bool:
        try:
            await self.client.views_open(trigger_id=trigger_id, view=view)
            return True
        except Exception:
            logger.warning("SlackInk: open_modal failed", exc_info=True)
            return False

    async def pin_message(self, channel: str, ts: str) -> bool:
        try:
            await self.client.pins_add(channel=channel, timestamp=ts)
            return True
        except SlackApiError as e:
            if e.response.get("error") == "already_pinned":
                logger.debug("SlackInk: pin_message skipped — message already pinned")
                return True
            logger.warning("SlackInk: pin_message failed", exc_info=True)
            return False
        except Exception:
            logger.warning("SlackInk: pin_message failed", exc_info=True)
            return False

    async def unpin_message(self, channel: str, ts: str) -> bool:
        try:
            await self.client.pins_remove(channel=channel, timestamp=ts)
            return True
        except SlackApiError as e:
            if e.response.get("error") == "not_pinned":
                logger.debug("SlackInk: unpin_message skipped — message not pinned")
                return True
            logger.warning(
                "SlackInk: unpin_message failed: %s",
                e.response.get("error", "unknown"),
                exc_info=True,
            )
            return False
        except Exception:
            logger.warning("SlackInk: unpin_message failed", exc_info=True)
            return False

    async def get_permalink(self, channel: str, ts: str) -> str | None:
        try:
            resp = await self.client.chat_getPermalink(channel=channel, message_ts=ts)
            return resp.get("permalink")
        except Exception:
            logger.warning("SlackInk: get_permalink failed", exc_info=True)
            return None

    async def bookmark_upsert(
        self,
        channel: str,
        title: str,
        link: str,
        existing_id: str | None = None,
    ) -> str | None:
        try:
            if existing_id:
                resp = await self.client.bookmarks_edit(
                    channel_id=channel,
                    bookmark_id=existing_id,
                    title=title,
                    link=link,
                )
                return resp.get("bookmark", {}).get("id") or existing_id
            else:
                resp = await self.client.bookmarks_add(
                    channel_id=channel,
                    title=title,
                    link=link,
                    type="link",
                )
                return resp.get("bookmark", {}).get("id")
        except SlackApiError as e:
            logger.warning(
                "SlackInk: bookmark_upsert failed: %s",
                e.response.get("error", "unknown"),
                exc_info=True,
            )
            return None
        except Exception:
            logger.warning("SlackInk: bookmark_upsert failed", exc_info=True)
            return None

    async def bookmark_remove(self, channel: str, bookmark_id: str) -> bool:
        try:
            await self.client.bookmarks_remove(
                channel_id=channel, bookmark_id=bookmark_id
            )
            return True
        except Exception:
            logger.warning("SlackInk: bookmark_remove failed", exc_info=True)
            return False

    async def set_title(self, channel: str, thread_ts: str, title: str) -> None:
        try:
            await self.client.api_call(
                "assistant.threads.setTitle",
                params={"channel_id": channel, "thread_ts": thread_ts, "title": title},
            )
        except Exception:
            logger.debug("SlackInk: set_title failed", exc_info=True)
