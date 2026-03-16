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
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
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
from pydantic import SecretStr

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
from octomate.tentacles.base import Mask, SendTarget, Tentacle
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class LarkTentacle(Tentacle):
    app_id: str
    app_secret: SecretStr
    _client: lark.Client
    _ws_client: lark.ws.Client | None
    _task: asyncio.Task[None] | None
    _loop: asyncio.AbstractEventLoop | None
    _queue: asyncio.Queue[MessageEvent]
    _current_message_id: str

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        *,
        app_id: str,
        app_secret: SecretStr,
        flush_delay: float = 0.5,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret.get_secret_value())
            .build()
        )
        self._ws_client = None
        self._task = None
        self._loop = None
        self._queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._current_message_id = ""
        super().__init__(tag, octopus, flush_delay=flush_delay)

    @cached_property
    def ink(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="https://open.feishu.cn/open-apis")

    def inspect(self) -> Mask:
        try:
            secret = self.app_secret.get_secret_value()
            resp = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": secret},
            )
            resp.raise_for_status()
            token = resp.json().get("tenant_access_token")
            if not token:
                return Mask(id="", name="")

            resp = httpx.get(
                "https://open.feishu.cn/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            bot = resp.json().get("bot", {})
            if bot:
                mask = Mask(
                    id=bot.get("open_id", ""),
                    name=bot.get("app_name", ""),
                )
                logger.info(
                    "Tentacle %s: probed as %s (%s)",
                    self.tag,
                    mask.id,
                    mask.name,
                )
                return mask
        except Exception:
            logger.warning(
                "Tentacle %s: probe failed, identity unknown",
                self.tag,
                exc_info=True,
            )
        return Mask(id="", name="")

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Lark WebSocket client", self.tag)
        self._loop = asyncio.get_running_loop()

        async def _consume_queue() -> None:
            while True:
                event = await self._queue.get()
                try:
                    await self.submerge(event)
                    self.buffer.push(event)
                except Exception:
                    logger.exception("Tentacle %s: error processing event", self.tag)

        async with asyncio.TaskGroup() as tg:
            self._task = tg.create_task(self._run_ws_client())
            tg.create_task(_consume_queue())

    async def deactivate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def splash(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        chat_id = str(target.chat_id)
        reply_id = str(target.reply_to) if target.reply_to else None
        is_group = target.chat_type == "group"

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
                apath = anyio.Path(seg.data.path)
                if not await apath.exists():
                    logger.warning(
                        "Tentacle %s: image file not found: %s", self.tag, apath
                    )
                    continue
                try:
                    image_data = await apath.read_bytes()
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
                    if resp.success() and resp.data and resp.data.image_key:
                        image_keys.append(resp.data.image_key)
                    else:
                        logger.warning(
                            "Tentacle %s: image upload failed: %s %s",
                            self.tag,
                            resp.code,
                            resp.msg,
                        )
                except Exception:
                    logger.warning(
                        "Tentacle %s: failed to upload image",
                        self.tag,
                        exc_info=True,
                    )

        receive_id_type = "chat_id" if is_group else "open_id"

        if text_parts:
            text = "".join(text_parts)
            content = json.dumps({"text": text})
            if message_id:
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .content(content)
                        .msg_type("text")
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
                        .receive_id(chat_id)
                        .msg_type("text")
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
                    self.tag,
                    resp.code,
                    resp.msg,
                )

        for key in image_keys:
            content = json.dumps({"image_key": key})
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("image")
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
                    "Tentacle %s: failed to send image: %s %s",
                    self.tag,
                    resp.code,
                    resp.msg,
                )

    async def secrete(self, seg: ImageSegment) -> None:
        pass

    async def submerge(self, event: MessageEvent) -> None:
        self._current_message_id = str(event.message_id)
        try:
            await super().submerge(event)
        finally:
            self._current_message_id = ""

    async def absorb(self, seg: ImageSegment, save_dir: Path) -> None:
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(self._current_message_id)
                .file_key(seg.data.file)
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
                    self.tag,
                    resp.code,
                    resp.msg,
                )
                return

            await anyio.Path(save_dir).mkdir(parents=True, exist_ok=True)
            file_name: str = resp.file_name or seg.data.file
            ext = guess_image_ext("", file_name)
            file_path = save_dir / f"{uuid.uuid4().hex}{ext}"
            if resp.file:
                data = resp.file.read()
                await anyio.Path(file_path).write_bytes(data)
            seg.data.file = str(file_path.resolve())
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.tag, exc_info=True
            )

    def _on_message_receive(self, data: Any) -> None:
        try:
            event = data.event
            message = event.message
            sender = event.sender

            msg_type: str = message.message_type
            content_json: str | None = message.content
            chat_type: str = message.chat_type
            mentions: list[Any] | None = message.mentions

            segments = self._parse_lark_content(msg_type, content_json, mentions)
            sender_id: str = sender.sender_id.open_id if sender.sender_id else ""
            sender_name: str = (
                sender.sender_id.open_id if sender.sender_id else "unknown"
            )

            now = int(time.time())
            result: MessageEvent | None = None

            if chat_type == "group":
                result = GroupMessageEvent(
                    time=now,
                    self_id=self.mask.id,
                    tentacle_id=self.tag,
                    message_id=message.message_id,
                    user_id=sender_id,
                    group_id=message.chat_id,
                    sender=Sender(user_id=sender_id, nickname=sender_name),
                    message=segments,
                    raw_message=content_json or "",
                )
            elif chat_type == "p2p":
                result = PrivateMessageEvent(
                    time=now,
                    self_id=self.mask.id,
                    tentacle_id=self.tag,
                    message_id=message.message_id,
                    user_id=sender_id,
                    sender=Sender(user_id=sender_id, nickname=sender_name),
                    message=segments,
                    raw_message=content_json or "",
                )

            if result and self._loop:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, result)
        except Exception:
            logger.warning(
                "Tentacle %s: failed to convert Lark event",
                self.tag,
                exc_info=True,
            )

    def _parse_lark_content(
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

    async def _run_ws_client(self) -> None:
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        await asyncio.to_thread(self._ws_client.start)
