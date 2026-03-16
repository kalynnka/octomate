"""Lark (Feishu) WebSocket tentacle — connects via lark-oapi SDK.

Uses the lark-oapi SDK's WebSocket client to receive events from Feishu,
converts them into the internal schema, and pushes them through the Nerve.
Outbound actions are sent via the Lark IM API.

Reference: https://open.feishu.cn/document/
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import lark_oapi as lark
from pydantic import SecretStr

from octomate.ink.lark import LarkInk
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
    ink: LarkInk
    _ws_client: lark.ws.Client | None
    _task: asyncio.Task[None] | None
    _loop: asyncio.AbstractEventLoop | None
    _queue: asyncio.Queue[MessageEvent]

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        *,
        app_id: str,
        app_secret: SecretStr,
        flush_delay: float = 0.5,
    ) -> None:
        self.ink = LarkInk(app_id, app_secret)
        self._ws_client = None
        self._task = None
        self._loop = None
        self._queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        super().__init__(tag, octopus, flush_delay=flush_delay)

    def inspect(self) -> Mask:
        mask = self.ink.inspect()
        if mask.id:
            logger.info("Tentacle %s: probed as %s (%s)", self.tag, mask.id, mask.name)
        else:
            logger.warning("Tentacle %s: probe failed, identity unknown", self.tag)
        return mask

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
                    image_key = await self.ink.upload_image(image_data)
                    if image_key:
                        image_keys.append(image_key)
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
                await self.ink.reply_message(message_id, "text", content)
            else:
                await self.ink.send_message(chat_id, receive_id_type, "text", content)

        for key in image_keys:
            content = json.dumps({"image_key": key})
            await self.ink.send_message(chat_id, receive_id_type, "image", content)

    async def secrete(self, seg: ImageSegment) -> None:
        pass

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        try:
            result = await self.ink.download_image(message_id, seg.data.file)
            if not result:
                return

            data, file_name = result
            await anyio.Path(save_dir).mkdir(parents=True, exist_ok=True)
            ext = guess_image_ext("", file_name)
            file_path = save_dir / f"{uuid.uuid4().hex}{ext}"
            if data:
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
            self.ink.app_id,
            self.ink.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        await asyncio.to_thread(self._ws_client.start)
