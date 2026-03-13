"""Lark (Feishu) WebSocket tentacle — connects via lark-oapi SDK.

Uses the lark-oapi SDK's WebSocket client to receive events from Feishu,
converts them into the internal schema, and pushes them through the Nerve.
Outbound actions are sent via the Lark IM API.

Reference: https://open.feishu.cn/document/
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from octomate.config import LarkTentacleConfig
from octomate.schemas.actions import (
    SendGroupMsgAction,
    SendPrivateMsgAction,
)
from octomate.schemas.adaptors import ActionUnion
from octomate.schemas.events import GroupMessageEvent, MessageEvent, PrivateMessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.schemas.session import Sender
from octomate.tentacles.base import BaseTentacle
from octomate.utils import guess_image_ext

logger = logging.getLogger(__name__)

FILES_ROOT = Path(".octomate/files")


class LarkTentacle(BaseTentacle):
    config: LarkTentacleConfig
    _client: lark.Client
    _ws_client: lark.ws.Client | None
    _task: asyncio.Task[None] | None
    _loop: asyncio.AbstractEventLoop | None
    _queue: asyncio.Queue[MessageEvent]

    def __init__(self, config: LarkTentacleConfig) -> None:
        super().__init__(config)
        self.config = config
        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret.get_secret_value())
            .build()
        )
        self._ws_client = None
        self._task = None
        self._loop = None
        self._queue: asyncio.Queue[MessageEvent] = asyncio.Queue()

    async def introspect(self) -> None:
        try:
            resp = await asyncio.to_thread(self._fetch_bot_info)
            if resp:
                self.self_id = resp.get("open_id", "")
                self.self_name = resp.get("app_name", "")
                logger.info(
                    "Tentacle %s: introspected as %s (%s)",
                    self.name,
                    self.self_id,
                    self.self_name,
                )
        except Exception:
            logger.warning(
                "Tentacle %s: introspection failed, identity unknown",
                self.name,
                exc_info=True,
            )

    def _fetch_bot_info(self) -> dict[str, Any] | None:
        secret = self.config.app_secret.get_secret_value()
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.config.app_id, "app_secret": secret},
        )
        resp.raise_for_status()
        token = resp.json().get("tenant_access_token")
        if not token:
            return None

        resp = httpx.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json().get("bot", {})

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Lark WebSocket client", self.name)
        await self.introspect()
        self._loop = asyncio.get_running_loop()

        async with asyncio.TaskGroup() as tg:
            self._task = tg.create_task(self._run_ws_client())
            tg.create_task(self._consume_queue())

    async def deactivate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def act(self, action: ActionUnion) -> None:
        if isinstance(action, SendGroupMsgAction):
            chat_id = str(action.params.group_id)
            reply_id = str(action.params.reply) if action.params.reply else None
            segments = action.params.message
        elif isinstance(action, SendPrivateMsgAction):
            chat_id = str(action.params.user_id)
            reply_id = str(action.params.reply) if action.params.reply else None
            segments = action.params.message
        else:
            logger.debug(
                "Tentacle %s: unsupported action type %s",
                self.name,
                type(action).__name__,
            )
            return

        reply_seg: ReplySegment | None = None
        remaining: list[AgentSegment] = []
        for seg in segments:
            if isinstance(seg, ReplySegment) and reply_seg is None:
                reply_seg = seg
            else:
                remaining.append(seg)

        message_id = reply_seg.data["id"] if reply_seg else reply_id

        text_parts: list[str] = []
        image_keys: list[str] = []

        for seg in remaining:
            if isinstance(seg, TextSegment):
                text_parts.append(seg.data["text"])
            elif isinstance(seg, AtSegment):
                text_parts.append(
                    f'<at user_id="{seg.data.user_id}">{seg.data.name or ""}</at>'
                )
            elif isinstance(seg, ImageSegment):
                key = await self._upload_image(seg)
                if key:
                    image_keys.append(key)

        if text_parts:
            text = "".join(text_parts)
            content = json.dumps({"text": text})
            if isinstance(action, SendGroupMsgAction):
                await self._send_message(
                    "chat_id", chat_id, "text", content, message_id
                )
            else:
                await self._send_message(
                    "open_id", chat_id, "text", content, message_id
                )

        for key in image_keys:
            content = json.dumps({"image_key": key})
            if isinstance(action, SendGroupMsgAction):
                await self._send_message("chat_id", chat_id, "image", content, None)
            else:
                await self._send_message("open_id", chat_id, "image", content, None)

    async def _send_message(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_message_id: str | None,
    ) -> None:
        if reply_message_id:
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type(msg_type)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.reply,  # type: ignore[union-attr]
                request,
            )
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.create,  # type: ignore[union-attr]
                request,
            )
        if not resp.success():
            logger.warning(
                "Tentacle %s: failed to send message: %s %s",
                self.name,
                resp.code,
                resp.msg,
            )

    async def _upload_image(self, seg: ImageSegment) -> str | None:
        path = seg.data.path
        if not path.exists():
            logger.warning("Tentacle %s: image file not found: %s", self.name, path)
            return None

        try:
            image_data = path.read_bytes()
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(image_data))
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.image.create,  # type: ignore[union-attr]
                request,
            )
            if resp.success() and resp.data:
                return resp.data.image_key
            logger.warning(
                "Tentacle %s: image upload failed: %s %s",
                self.name,
                resp.code,
                resp.msg,
            )
        except Exception:
            logger.warning(
                "Tentacle %s: failed to upload image", self.name, exc_info=True
            )
        return None

    async def _download_image(
        self, message_id: str, image_key: str, save_dir: Path
    ) -> str | None:
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message_resource.get,  # type: ignore[union-attr]
                request,
            )
            if not resp.success():
                logger.warning(
                    "Tentacle %s: image download failed: %s %s",
                    self.name,
                    resp.code,
                    resp.msg,
                )
                return None

            save_dir.mkdir(parents=True, exist_ok=True)
            file_name: str = resp.file_name or image_key
            ext = guess_image_ext("", file_name)
            file_path = save_dir / f"{uuid.uuid4().hex}{ext}"
            if resp.file:
                file_path.write_bytes(resp.file.read())
            return str(file_path.resolve())
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.name, exc_info=True
            )
            return None

    def _on_message_receive(self, data: Any) -> None:
        try:
            event = self._convert_lark_event(data)
            if event and self._loop:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except Exception:
            logger.warning(
                "Tentacle %s: failed to convert Lark event", self.name, exc_info=True
            )

    def _convert_lark_event(self, data: Any) -> MessageEvent | None:
        event = data.event
        message = event.message
        sender = event.sender

        msg_type: str = message.message_type
        content_json: str | None = message.content
        chat_type: str = message.chat_type

        segments = self._parse_content(msg_type, content_json, message.mentions)
        sender_id: str = sender.sender_id.open_id if sender.sender_id else ""
        sender_name: str = sender.sender_id.open_id if sender.sender_id else "unknown"

        now = int(time.time())

        if chat_type == "group":
            return GroupMessageEvent(
                time=now,
                self_id=self.self_id or "",
                tentacle_id=self.name,
                message_id=message.message_id,
                user_id=sender_id,
                group_id=message.chat_id,
                sender=Sender(user_id=sender_id, nickname=sender_name),
                message=segments,
                raw_message=content_json or "",
            )
        elif chat_type == "p2p":
            return PrivateMessageEvent(
                time=now,
                self_id=self.self_id or "",
                tentacle_id=self.name,
                message_id=message.message_id,
                user_id=sender_id,
                sender=Sender(user_id=sender_id, nickname=sender_name),
                message=segments,
                raw_message=content_json or "",
            )
        return None

    def _parse_content(
        self,
        msg_type: str,
        content_json: str | None,
        mentions: list[Any] | None,
    ) -> list[MessageSegment]:
        if not content_json:
            return []

        try:
            content: dict[str, Any] = json.loads(content_json)
        except (json.JSONDecodeError, TypeError):
            return [TextSegment(data={"text": content_json})]

        segments: list[MessageSegment] = []

        if msg_type == "text":
            text: str = content.get("text", "")
            if mentions:
                for m in mentions:
                    placeholder: str = m.key
                    if placeholder in text:
                        before, _, after = text.partition(placeholder)
                        if before:
                            segments.append(TextSegment(data={"text": before}))
                        segments.append(
                            AtSegment(
                                data=AtData(user_id=m.id.open_id or m.key, name=m.name)
                            )
                        )
                        text = after
                if text:
                    segments.append(TextSegment(data={"text": text}))
            else:
                segments.append(TextSegment(data={"text": text}))

        elif msg_type == "image":
            image_key: str = content.get("image_key", "")
            segments.append(
                ImageSegment(data=ImageData(file=image_key, name=image_key))
            )

        elif msg_type == "post":
            title: str = content.get("title", "")
            if title:
                segments.append(TextSegment(data={"text": f"[{title}]\n"}))
            for lang_content in content.values():
                if isinstance(lang_content, list):
                    for line in lang_content:
                        for element in line:
                            tag: str = element.get("tag", "")
                            if tag == "text":
                                segments.append(
                                    TextSegment(data={"text": element.get("text", "")})
                                )
                            elif tag == "a":
                                segments.append(
                                    TextSegment(data={"text": element.get("href", "")})
                                )
                            elif tag == "at":
                                segments.append(
                                    AtSegment(
                                        data=AtData(
                                            user_id=element.get("user_id", ""),
                                            name=element.get("user_name", ""),
                                        )
                                    )
                                )
                            elif tag == "img":
                                segments.append(
                                    ImageSegment(
                                        data=ImageData(
                                            file=element.get("image_key", ""),
                                            name=element.get("image_key", ""),
                                        )
                                    )
                                )
                    break

        else:
            segments.append(TextSegment(data={"text": f"[{msg_type}]"}))

        return segments

    async def prepare_inbound_message(self, event: MessageEvent) -> None:
        subdir = (
            f"g{event.group_id}"
            if isinstance(event, GroupMessageEvent)
            else f"u{event.user_id}"
        )
        save_dir = FILES_ROOT / self.name / subdir

        for seg in event.message:
            if isinstance(seg, ImageSegment) and not seg.data.path.exists():
                image_key = seg.data.file
                local_path = await self._download_image(
                    str(event.message_id),
                    image_key,
                    save_dir,
                )
                if local_path:
                    seg.data.file = local_path

    async def _run_ws_client(self) -> None:
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        await asyncio.to_thread(self._ws_client.start)

    async def _consume_queue(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if isinstance(event, MessageEvent):
                    await self.prepare_inbound_message(event)
                await self.nerve.sense(event)
            except Exception:
                logger.exception("Tentacle %s: error processing event", self.name)
