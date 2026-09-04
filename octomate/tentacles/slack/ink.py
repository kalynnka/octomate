from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

import aiohttp
import httpx
from pydantic import SecretStr
from slack_sdk.models.messages.chunk import Chunk
from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient

from octomate.schemas.segments import ImageSegment
from octomate.telemetry import slack_logfire
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.feelers.output import IMMessageID, MarkdownChunker
from octomate.tentacles.slack.schema import (
    SlackBlock,
    SlackOutboundMessage,
    SlackPostMessageKwargs,
    SlackUpdateMessageKwargs,
    SlackUserProfile,
)

logger = logging.getLogger(__name__)


STREAM_BUFFER_SIZE = 1024
LONG_MARKDOWN_FILENAME = "octomate-response.md"
LONG_MARKDOWN_NOTE = (
    "Response was too long for a Slack message, so I uploaded it as a Markdown file."
)

SLACK_MARKDOWN_TEXT_LIMIT = 12_000
SLACK_MARKDOWN_CHUNKER = MarkdownChunker(limit=SLACK_MARKDOWN_TEXT_LIMIT)


class SlackInk(Ink[SlackOutboundMessage]):
    def __init__(self, bot_token: SecretStr) -> None:
        self.bot_token = bot_token
        token = bot_token.get_secret_value()
        self.client = AsyncWebClient(token=token)

    async def __aenter__(self) -> Self:
        """Open the pooled aiohttp session every Web API call rides on. Without
        a running session slack_sdk opens a fresh connection — TCP + TLS
        handshake — per call, which priced every streaming flush at a full
        connection setup."""
        self.client.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.client.timeout)
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.client.session is not None:
            await self.client.session.close()
            self.client.session = None

    async def inspect(self) -> SlackUserProfile:
        resp = await self.client.auth_test()
        bot_user_id = resp.get("user_id", "")
        user_resp = await self.client.users_info(user=bot_user_id)
        user = user_resp.get("user", {})
        profile = user.get("profile", {})
        return SlackUserProfile(
            channel_user_id=bot_user_id,
            name=profile.get("real_name") or user.get("real_name", bot_user_id),
            nickname=profile.get("display_name") or None,
            title=profile.get("title") or None,
        )

    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None:
        """The `D…` channel id of the bot's 1:1 with `user_id`.

        `conversations.open` is idempotent — it returns the existing DM when there is
        one — so this never creates a second surface for the same pair.
        """
        try:
            resp = await self.client.conversations_open(users=user_id)
        except Exception:
            # Usually a missing `im:write` scope, which would otherwise degrade every
            # DM move with nothing to diagnose it by.
            logger.warning(
                "SlackInk: conversations.open failed for %s",
                user_id,
                exc_info=True,
            )
            return None
        channel = resp.get("channel") or {}
        return channel.get("id") or None

    async def get_user_profile(self, user_id: str) -> SlackUserProfile:
        try:
            resp = await self.client.users_info(user=user_id)
            user = resp.get("user", {})
            profile = user.get("profile", {})
            return SlackUserProfile(
                channel_user_id=user_id,
                name=profile.get("real_name") or user.get("real_name", user_id),
                nickname=profile.get("display_name") or None,
                title=profile.get("title") or None,
            )
        except Exception:
            logger.warning(
                "SlackInk: get_user_profile failed for %s", user_id, exc_info=True
            )
            return SlackUserProfile(channel_user_id=user_id, name=user_id)

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
    ) -> IMMessageID | None:
        first_msg_id: IMMessageID | None = None
        thread_ts = reply_to
        for msg in messages:
            try:
                markdown_text = msg.markdown_text or msg.text
                if len(markdown_text) > SLACK_MARKDOWN_TEXT_LIMIT:
                    result = await self.upload_markdown_file(
                        channel=chat_id,
                        markdown_text=markdown_text,
                        thread_ts=thread_ts,
                    )
                    first_msg_id = first_msg_id or result
                else:
                    kwargs: SlackPostMessageKwargs = {
                        "channel": chat_id,
                        "text": msg.text,
                    }
                    if msg.blocks:
                        kwargs["blocks"] = msg.blocks
                    if thread_ts:
                        kwargs["thread_ts"] = thread_ts
                    resp = await self.client.chat_postMessage(**kwargs)
                    first_msg_id = first_msg_id or resp.get("ts")
            except Exception:
                logger.warning("SlackInk: send_message failed", exc_info=True)
            if not reply_in_thread:
                thread_ts = None
        return first_msg_id

    # Marks where a stream opens, not a round-trip: `chat_stream` only builds the
    # `AsyncChatStream`, and `chat.startStream` is paid by the first append.
    # `task_display_mode` is what tells a plan stream from a text one.
    @slack_logfire.instrument(
        "slack.start_stream", extract_args=["channel", "task_display_mode"]
    )
    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncChatStream:
        return await self.client.chat_stream(
            buffer_size=STREAM_BUFFER_SIZE,
            channel=channel,
            thread_ts=thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
            task_display_mode=task_display_mode,
        )

    async def append_stream(
        self,
        stream: AsyncChatStream,
        markdown_text: str,
    ) -> None:
        with slack_logfire.span("slack.append_stream", chars=len(markdown_text)):
            for chunk in SLACK_MARKDOWN_CHUNKER.chunk(markdown_text):
                # `chunks=()` forces slack-sdk to flush its internal text buffer.
                await stream.append(markdown_text=chunk, chunks=())

    async def append_stream_chunks(
        self,
        stream: AsyncChatStream,
        chunks: list[Chunk],
    ) -> None:
        with slack_logfire.span("slack.append_stream_chunks", chunks=len(chunks)):
            await stream.append(chunks=chunks)

    @asynccontextmanager
    async def open_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncIterator[AsyncChatStream]:
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
            task_display_mode=task_display_mode,
        )
        try:
            yield stream
        finally:
            try:
                await self.stop_stream(stream)
            except Exception:
                logger.warning("SlackInk: failed to stop stream", exc_info=True)

    @slack_logfire.instrument("slack.stop_stream", extract_args=False)
    async def stop_stream(
        self,
        stream: AsyncChatStream,
        *,
        markdown_text: str | None = None,
    ) -> IMMessageID | None:
        if markdown_text and len(markdown_text) > SLACK_MARKDOWN_TEXT_LIMIT:
            await self.append_stream(stream, markdown_text)
            markdown_text = None
        resp = await stream.stop(markdown_text=markdown_text)
        return resp.get("ts")

    async def stream_markdown(
        self,
        channel: str,
        thread_ts: str,
        markdown_text: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> IMMessageID | None:
        if len(markdown_text) > SLACK_MARKDOWN_TEXT_LIMIT:
            return await self.upload_markdown_file(
                channel=channel,
                markdown_text=markdown_text,
                thread_ts=thread_ts,
            )
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
        )
        return await self.stop_stream(stream, markdown_text=markdown_text)

    async def upload_image(
        self,
        *,
        channel: str,
        data: bytes,
        filename: str,
        thread_ts: str | None = None,
    ) -> IMMessageID | None:
        """Upload image bytes shared directly into the channel/thread."""
        resp = await self.client.files_upload_v2(
            channel=channel,
            content=data,
            filename=filename,
            thread_ts=thread_ts,
        )
        file_info = resp.get("file", {})
        return (
            file_info.get("permalink")
            or file_info.get("url_private")
            or resp.get("file_id")
        )

    async def upload_markdown_file(
        self,
        *,
        channel: str,
        markdown_text: str,
        thread_ts: str | None = None,
        filename: str = LONG_MARKDOWN_FILENAME,
    ) -> IMMessageID | None:
        resp = await self.client.files_upload_v2(
            channel=channel,
            content=markdown_text,
            filename=filename,
            title="Octomate response",
            snippet_type="markdown",
            initial_comment=LONG_MARKDOWN_NOTE,
            thread_ts=thread_ts,
        )
        file_info = resp.get("file", {})
        return (
            file_info.get("permalink")
            or file_info.get("url_private")
            or resp.get("file_id")
        )

    async def update_message(
        self,
        channel: str,
        message_id: str,
        text: str = "",
        blocks: list[SlackBlock] | None = None,
    ) -> bool:
        try:
            kwargs: SlackUpdateMessageKwargs = {
                "channel": channel,
                "ts": message_id,
                "text": text,
            }
            if blocks:
                kwargs["blocks"] = blocks
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
