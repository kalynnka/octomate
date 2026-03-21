"""Lark (Feishu) WebSocket tentacle — connects via lark-oapi SDK."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import anyio.to_thread
import lark_oapi
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import SecretStr

from octomate.schemas.actions import ConfirmAction
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.base import SendTarget, Tentacle, PlatformMessage
from octomate.tentacles.lark.chromo import LarkChromo
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
        self.chromo = LarkChromo()
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

    def sense(self, data: P2ImMessageReceiveV1) -> None:
        anyio.from_thread.run(self.ingest, data)

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
    ) -> bool:
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        for msg in messages:
            if reply_to:
                await self.ink.reply_message(reply_to, msg.msg_type, msg.content)
                reply_to = None
            else:
                await self.ink.send_message(chat_id, receive_id_type, msg.msg_type, msg.content)
        return True

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

    def on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            event = data.event
            if event is None or event.action is None:
                return P2CardActionTriggerResponse({})

            value: dict = event.action.value or {}
            if value.get("action") != "hitl_confirm":
                return P2CardActionTriggerResponse({})

            confirmation_id = value.get("confirmation_id", "")
            approved = value.get("approved", "") == "true"

            entry = self.octopus.confirmations.pending.get(confirmation_id)
            if entry is None:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "Already handled"}}
                )

            action, _ = entry
            if action.approvers:
                clicker_id = ""
                if event.operator:
                    clicker_id = event.operator.open_id or ""
                if clicker_id not in action.approvers:
                    return P2CardActionTriggerResponse(
                        {
                            "toast": {
                                "type": "warning",
                                "content": "You are not authorized to approve this action",
                            }
                        }
                    )

            resolved = anyio.from_thread.run(
                self.octopus.confirm, confirmation_id, approved
            )

            if resolved:
                msg = "Approved" if approved else "Denied"
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": msg}}
                )
            return P2CardActionTriggerResponse(
                {"toast": {"type": "warning", "content": "Already handled"}}
            )
        except Exception:
            logger.warning(
                "Tentacle %s: failed to handle card action",
                self.tag,
                exc_info=True,
            )
            return P2CardActionTriggerResponse({})

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

        approve_value = {
            "action": "hitl_confirm",
            "confirmation_id": action.confirmation_id,
            "approved": "true",
        }
        deny_value = {
            "action": "hitl_confirm",
            "confirmation_id": action.confirmation_id,
            "approved": "false",
        }
        card = json.dumps(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Action Confirmation"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**Tool:** {action.tool_name}\n"
                            f"**Description:** {description}\n"
                            f"**Arguments:**\n```json\n{args_json}\n```" + mention_line
                        ),
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Approve"},
                                "type": "primary",
                                "value": approve_value,
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "Deny"},
                                "type": "danger",
                                "value": deny_value,
                            },
                        ],
                    },
                ],
            }
        )

        return await self.ink.send_message(
            chat_id, receive_id_type, "interactive", card
        )
