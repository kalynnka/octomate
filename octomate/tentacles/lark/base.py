"""Lark (Feishu) WebSocket tentacle — connects via lark-oapi SDK.

Uses the lark-oapi SDK's WebSocket client to receive events from Feishu,
converts them into the internal schema, and pushes them through the Nerve.
Outbound actions are sent via the Lark IM API.

Reference: https://open.feishu.cn/document/
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.from_thread
import anyio.to_thread
import lark_oapi
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import SecretStr

from octomate.schemas.actions import ConfirmAction
from octomate.schemas.events import GroupMessageEvent, MessageEvent, PrivateMessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.base import SendTarget, Tentacle
from octomate.tentacles.lark.ink import LarkInk
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class LarkTentacle(Tentacle):
    ws_client: lark_oapi.ws.Client
    ws_scope: anyio.CancelScope | None

    ink: LarkInk

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
        event_handler = (
            lark_oapi.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.sense)
            .register_p2_card_action_trigger(self.on_card_action)
            .build()
        )
        self.ws_client = lark_oapi.ws.Client(
            self.ink.app_id,
            self.ink.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark_oapi.LogLevel.INFO,
        )
        self.ws_scope = None
        super().__init__(tag, octopus, flush_delay=flush_delay)

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Lark WebSocket client", self.tag)
        with anyio.CancelScope() as scope:
            self.ws_scope = scope
            await anyio.to_thread.run_sync(self.ws_client.start)

    async def deactivate(self) -> None:
        if self.ws_scope is not None:
            self.ws_scope.cancel()

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
        receive_id_type = "chat_id" if is_group else "open_id"

        for seg in remaining:
            msg_type: str | None = None
            content: str | None = None

            if isinstance(seg, MarkdownSegment):
                msg_type = "interactive"
                elements = [{"tag": "markdown", "content": seg.data["text"]}]
                content = json.dumps({"schema": "2.0", "body": {"elements": elements}})
            elif isinstance(seg, TextSegment):
                msg_type = "text"
                content = json.dumps({"text": seg.data["text"]})
            elif isinstance(seg, AtSegment):
                msg_type = "text"
                at_text = f'<at user_id="{seg.data.user_id}">{seg.data.name or ""}</at>'
                content = json.dumps({"text": at_text})
            elif isinstance(seg, ImageSegment) and seg.data.url:
                msg_type = "image"
                content = json.dumps({"image_key": seg.data.url})

            if msg_type and content:
                if message_id:
                    await self.ink.reply_message(message_id, msg_type, content)
                    message_id = None
                else:
                    await self.ink.send_message(
                        chat_id, receive_id_type, msg_type, content
                    )

    async def secrete(self, seg: ImageSegment) -> None:
        apath = anyio.Path(seg.data.path)
        if not await apath.exists():
            logger.warning("Tentacle %s: image file not found: %s", self.tag, apath)
            return
        try:
            image_data = await apath.read_bytes()
            image_key = await self.ink.upload_image(image_data)
            if image_key:
                seg.data.url = image_key
        except Exception:
            logger.warning(
                "Tentacle %s: failed to upload image", self.tag, exc_info=True
            )

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

    def sense(self, data: P2ImMessageReceiveV1) -> None:
        """Lark SDK event callback: convert a raw Lark event into a MessageEvent and ingest it."""
        try:
            event = data.event
            if event is None:
                logger.warning("Tentacle %s: received event with no payload", self.tag)
                return

            message = event.message
            sender = event.sender
            if message is None or sender is None:
                logger.warning(
                    "Tentacle %s: event missing message or sender (message=%s, sender=%s)",
                    self.tag,
                    message,
                    sender,
                )
                return

            msg_type = message.message_type
            chat_type = message.chat_type
            if not msg_type or not chat_type:
                logger.warning(
                    "Tentacle %s: message missing type info (message_type=%s, chat_type=%s)",
                    self.tag,
                    msg_type,
                    chat_type,
                )
                return

            content_json: str | None = message.content
            mentions: list[Any] | None = message.mentions

            segments = self.digest(msg_type, content_json, mentions)

            sender_id_obj = sender.sender_id
            sender_id: str = (sender_id_obj.open_id or "") if sender_id_obj else ""

            sender_profile = anyio.from_thread.run(self.get_user_profile, sender_id)

            now = int(time.time())
            message_id: str = message.message_id or ""
            message_event: MessageEvent | None = None

            if chat_type == "group":
                message_event = GroupMessageEvent(
                    time=now,
                    self_id=self.profile.user_id,
                    tentacle_id=self.tag,
                    message_id=message_id,
                    user_id=sender_id,
                    group_id=message.chat_id or "",
                    sender=sender_profile,
                    message=segments,
                    raw_message=content_json or "",
                )
            elif chat_type == "p2p":
                message_event = PrivateMessageEvent(
                    time=now,
                    self_id=self.profile.user_id,
                    tentacle_id=self.tag,
                    message_id=message_id,
                    user_id=sender_id,
                    sender=sender_profile,
                    message=segments,
                    raw_message=content_json or "",
                )
            else:
                logger.warning(
                    "Tentacle %s: unsupported chat_type %r, message_id=%s",
                    self.tag,
                    chat_type,
                    message_id,
                )
                return

            if message_event:
                anyio.from_thread.run(self.submerge, message_event)
                self.buffer.push(message_event)
        except Exception:
            logger.warning(
                "Tentacle %s: failed to convert Lark event",
                self.tag,
                exc_info=True,
            )

    def digest(
        self,
        msg_type: str,
        content_json: str | None,
        mentions: list[Any] | None,
    ) -> list[MessageSegment]:
        """Break down raw Lark message content into a list of MessageSegments."""
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
                    placeholder: str | None = m.key
                    if not placeholder or placeholder not in text:
                        continue
                    before, _, after = text.partition(placeholder)
                    if before:
                        segments.append(TextSegment(data={"text": before}))
                    m_id = m.id
                    user_id = (m_id.open_id if m_id else None) or placeholder
                    segments.append(
                        AtSegment(data=AtData(user_id=user_id, name=m.name or ""))
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

    def on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """Lark SDK callback: handle interactive card button clicks."""
        resp = P2CardActionTriggerResponse()
        try:
            event = data.event
            if event is None or event.action is None:
                return resp

            value: dict = event.action.value or {}
            if value.get("action") != "hitl_confirm":
                return resp

            confirmation_id = value.get("confirmation_id", "")
            approved = bool(value.get("approved", False))

            entry = self.octopus.confirmations.pending.get(confirmation_id)
            if entry is None:
                toast = CallBackToast()
                toast.type = "warning"
                toast.content = "Already handled"
                resp.toast = toast
                return resp

            action, _ = entry
            if action.approvers:
                clicker_id = ""
                if event.operator:
                    clicker_id = event.operator.open_id or ""
                if clicker_id not in action.approvers:
                    toast = CallBackToast()
                    toast.type = "warning"
                    toast.content = "You are not authorized to approve this action"
                    resp.toast = toast
                    return resp

            resolved = anyio.from_thread.run(
                self.octopus.confirm, confirmation_id, approved
            )

            toast = CallBackToast()
            toast.type = "info" if resolved else "warning"
            toast.content = (
                ("Approved" if approved else "Denied")
                if resolved
                else "Already handled"
            )
            resp.toast = toast
        except Exception:
            logger.warning(
                "Tentacle %s: failed to handle card action",
                self.tag,
                exc_info=True,
            )
        return resp

    async def send_confirmation(
        self, target: SendTarget, action: ConfirmAction
    ) -> bool:
        chat_id = str(target.chat_id)
        is_group = target.chat_type == "group"
        receive_id_type = "chat_id" if is_group else "open_id"

        args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
        description = action.description or action.tool_name

        mention_line = ""
        if action.approvers:
            mentions = " ".join(f'<at id="{uid}"></at>' for uid in action.approvers)
            mention_line = f"\n**Approvers:** {mentions}"

        card = json.dumps(
            {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": "Action Confirmation"},
                    "template": "orange",
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"**Tool:** {action.tool_name}\n"
                                f"**Description:** {description}\n"
                                f"**Arguments:**\n```json\n{args_json}\n```"
                                + mention_line
                            ),
                        },
                        {
                            "tag": "action",
                            "actions": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Approve"},
                                    "type": "primary",
                                    "value": {
                                        "action": "hitl_confirm",
                                        "confirmation_id": action.confirmation_id,
                                        "approved": True,
                                    },
                                },
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Deny"},
                                    "type": "danger",
                                    "value": {
                                        "action": "hitl_confirm",
                                        "confirmation_id": action.confirmation_id,
                                        "approved": False,
                                    },
                                },
                            ],
                        },
                    ],
                },
            }
        )

        return await self.ink.send_message(
            chat_id, receive_id_type, "interactive", card
        )
