"""Lark (Feishu) WebSocket tentacle — connects via lark-oapi SDK."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.from_thread
import anyio.to_thread
import lark_oapi
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import SessionContext
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.base import ChannelTentacle, PlatformMessage
from octomate.tentacles.feelers import Feelers
from octomate.tentacles.lark.chromo import LarkChromo
from octomate.tentacles.lark.feelers import (
    LarkConfirmationFeeler,
    LarkQuestionFeeler,
    LarkTodoFeeler,
)
from octomate.tentacles.lark.ink import LarkInk
from octomate.utils import guess_image_ext

# Octopus imports tentacles (via connect_tentacles) so importing it here would
# be circular at module load time.
if TYPE_CHECKING:
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class LarkTentacle(ChannelTentacle):
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
        flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        memory: OctopusMemory,
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
        super().__init__(tag, octopus, flick, memory, flush_delay=flush_delay)

        self.feelers = Feelers(
            confirm=LarkConfirmationFeeler(self.ink, self.interactions),
            todos=LarkTodoFeeler(self.ink, self.interactions),
            questions=LarkQuestionFeeler(self.ink, self.interactions),
        )

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Lark WebSocket client", self.id)
        with anyio.CancelScope() as scope:
            self.ws_scope = scope
            await anyio.to_thread.run_sync(self.ws_client.start)

    async def deactivate(self) -> None:
        if self.ws_scope is not None:
            self.ws_scope.cancel()

    def sense(self, data: P2ImMessageReceiveV1) -> None:
        anyio.from_thread.run(self.ingest, data)

    async def secrete(self, seg: ImageSegment) -> None:
        apath = anyio.Path(seg.data.path)
        if not await apath.exists():
            logger.warning("Tentacle %s: image file not found: %s", self.id, apath)
            return
        try:
            image_data = await apath.read_bytes()
            image_key = await self.ink.upload_media(image_data)
            if image_key:
                seg.data.url = image_key
        except Exception:
            logger.warning(
                "Tentacle %s: failed to upload image", self.id, exc_info=True
            )

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        try:
            result = await self.ink.download_media(message_id, file_key=seg.data.file)
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
                "Tentacle %s: failed to download image", self.id, exc_info=True
            )

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        first_msg_id: str | None = None
        for msg in messages:
            if reply_to:
                msg_id = await self.ink.reply_message(
                    reply_to,
                    msg.msg_type,
                    msg.content,
                    reply_in_thread=reply_in_thread,
                )
                if not reply_in_thread:
                    reply_to = None
            else:
                msg_id = await self.ink.send_message(
                    chat_id, receive_id_type, msg.msg_type, msg.content
                )
            if first_msg_id is None:
                first_msg_id = msg_id
        return first_msg_id

    def on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            event = data.event
            if event is None or event.action is None:
                return P2CardActionTriggerResponse({})

            value: dict = event.action.value or {}
            action_type = value.get("action")
            clicker_id: str = (event.operator.open_id or "") if event.operator else ""

            if action_type == "confirm":
                confirmation_id = value.get("confirmation_id", "")
                approved = value.get("approved", "") == "true"

                entry = self.interactions.confirmations.get(confirmation_id)
                if entry is None:
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "warning", "content": "Already handled"}}
                    )
                action, _ = entry
                if action.approvers and clicker_id not in action.approvers:
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "warning", "content": "Not authorized"}}
                    )

                resolved = anyio.from_thread.run(
                    self.interactions.resolve_confirmation, confirmation_id, approved
                )
                if not resolved:
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "warning", "content": "Already handled"}}
                    )

                tool_name = value.get("tool_name", "")
                title = value.get("title") or tool_name
                status_emoji = "✅" if approved else "❌"
                status_text = "Approved" if approved else "Denied"
                template = "green" if approved else "red"
                return P2CardActionTriggerResponse(
                    {
                        "toast": {"type": "info", "content": status_text},
                        "card": {
                            "type": "raw",
                            "data": {
                                "header": {
                                    "title": {
                                        "tag": "plain_text",
                                        "content": f"{status_emoji} {status_text}",
                                    },
                                    "template": template,
                                },
                                "elements": [
                                    {
                                        "tag": "markdown",
                                        "content": f"**{title}**\n\n{status_emoji} {status_text}",
                                    },
                                ],
                            },
                        },
                    }
                )

            if action_type == "question_answer":
                form_value = event.action.form_value or {}
                answer = form_value.get("answer") or value.get("answer", "")
                question_id = value.get("question_id", "")
                entry = self.interactions.questions.get(question_id)
                question_text = entry[0].text if entry else ""
                anyio.from_thread.run(
                    self.interactions.resolve_question,
                    question_id,
                    answer,
                    clicker_id,
                )
                return P2CardActionTriggerResponse(
                    {
                        "toast": {"type": "info", "content": "Answer recorded"},
                        "card": {
                            "type": "raw",
                            "data": {
                                "header": {
                                    "title": {
                                        "tag": "plain_text",
                                        "content": "✅ Answered",
                                    },
                                    "template": "green",
                                },
                                "elements": [
                                    {
                                        "tag": "markdown",
                                        "content": question_text,
                                    },
                                    {"tag": "hr"},
                                    {
                                        "tag": "markdown",
                                        "content": f"💬 **Answer:** {answer}",
                                    },
                                ],
                            },
                        },
                    }
                )

            if action_type == "todo_update":
                todo_id = value.get("todo_id", "")
                status = value.get("status", "completed")
                title = value.get("title", "")
                anyio.from_thread.run(
                    self.interactions.update_todo,
                    todo_id,
                    status,
                )
                status_icon = "✅" if status == "completed" else "❌"
                status_label = "Done" if status == "completed" else "Cancelled"
                template = "green" if status == "completed" else "grey"
                return P2CardActionTriggerResponse(
                    {
                        "card": {
                            "type": "raw",
                            "data": {
                                "header": {
                                    "title": {
                                        "tag": "plain_text",
                                        "content": f"{status_icon} {status_label}",
                                    },
                                    "template": template,
                                },
                                "elements": [
                                    {
                                        "tag": "markdown",
                                        "content": f"{status_icon} ~~{title}~~ — **{status_label}**",
                                    },
                                ],
                            },
                        },
                    }
                )

        except Exception:
            logger.warning(
                "Tentacle %s: failed to handle card action", self.id, exc_info=True
            )
        return P2CardActionTriggerResponse({})
