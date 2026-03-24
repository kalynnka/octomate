from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from octomate.agents.base import SessionContext
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import ImageSegment
from octomate.store import InteractionStore
from octomate.tentacles.base import PlatformMessage, Tentacle
from octomate.tentacles.feelers import Feelers
from octomate.tentacles.slack.chromo import SlackChromo
from octomate.tentacles.slack.feelers import (
    SlackConfirmationFeeler,
    SlackQuestionFeeler,
    SlackTodoFeeler,
)
from octomate.tentacles.slack.ink import SlackInk
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

_IGNORED_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
    }
)


class SlackTentacle(Tentacle):
    app: AsyncApp
    handler: AsyncSocketModeHandler | None
    ink: SlackInk
    store: InteractionStore
    app_token: str

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        *,
        bot_token: SecretStr,
        app_token: SecretStr,
        store: InteractionStore,
        flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        self.ink = SlackInk(bot_token)
        self.chromo = SlackChromo()
        self.store = store

        self.feelers = Feelers(
            confirm=SlackConfirmationFeeler(self.ink, self.store),
            todos=SlackTodoFeeler(self.ink, self.store),
            questions=SlackQuestionFeeler(self.ink, self.store),
        )

        self.app = AsyncApp(token=bot_token.get_secret_value())
        self.app.event("message")(self.on_message)
        self.app.event("assistant_thread_started")(self.on_assistant_thread_started)
        self.app.event("assistant_thread_context_changed")(self._ack_noop)
        self.app.action(re.compile(".*"))(self.on_block_action)

        self.app_token = app_token.get_secret_value()
        self.handler = None

        super().__init__(tag, octopus, flick, memory, flush_delay=flush_delay)

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Slack Socket Mode client", self.tag)
        self.handler = AsyncSocketModeHandler(self.app, self.app_token)
        await self.handler.start_async()

    async def deactivate(self) -> None:
        if self.handler:
            await self.handler.close_async()

    async def on_message(self, event: dict, say) -> None:
        subtype = event.get("subtype")
        if subtype in _IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self.id:
            return
        await self.ingest(event)

    async def on_assistant_thread_started(self, event: dict, say) -> None:
        pass

    async def _ack_noop(self, event: dict) -> None:
        pass

    async def on_block_action(self, ack, body: dict) -> None:
        await ack()
        try:
            actions = body.get("actions", [])
            if not actions:
                return

            action_data = actions[0]
            value_str = action_data.get("value", "{}")
            try:
                value = json.loads(value_str)
            except (json.JSONDecodeError, TypeError):
                return

            action_type = value.get("action")
            clicker_id = body.get("user", {}).get("id", "")
            channel = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")

            if action_type == "confirm":
                confirmation_id = value.get("confirmation_id", "")
                approved = value.get("approved", "") == "true"

                entry = self.store.confirmations.get(confirmation_id)
                if entry is None:
                    return
                action, _ = entry
                if action.approvers and clicker_id not in action.approvers:
                    return

                resolved = self.store.resolve_confirmation(confirmation_id, approved)
                if not resolved:
                    return

                title = value.get("title") or value.get("tool_name", "")
                status_emoji = ":white_check_mark:" if approved else ":x:"
                status_text = "Approved" if approved else "Denied"
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{status_emoji} *{title}* — {status_text}",
                        },
                    }
                ]
                if channel and message_ts:
                    await self.ink.update_message(
                        channel,
                        message_ts,
                        text=f"{title} — {status_text}",
                        blocks=blocks,
                    )

            elif action_type == "question_answer":
                answer = value.get("answer", "")
                question_id = value.get("question_id", "")
                self.store.resolve_question(question_id, answer, clicker_id)

                entry = self.store.questions.get(question_id)
                question_text = entry[0].text if entry else ""
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":white_check_mark: *Answered*\n{question_text}\n\n:speech_balloon: *Answer:* {answer}",
                        },
                    }
                ]
                if channel and message_ts:
                    await self.ink.update_message(
                        channel, message_ts, text=f"Answered: {answer}", blocks=blocks
                    )

            elif action_type == "todo_update":
                todo_id = value.get("todo_id", "")
                status = value.get("status", "done")
                title = value.get("title", "")
                self.store.update_todo(todo_id, status)

                status_icon = ":white_check_mark:" if status == "done" else ":x:"
                status_label = "Done" if status == "done" else "Cancelled"
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{status_icon} ~{title}~ — *{status_label}*",
                        },
                    }
                ]
                if channel and message_ts:
                    await self.ink.update_message(
                        channel,
                        message_ts,
                        text=f"{title} — {status_label}",
                        blocks=blocks,
                    )

        except Exception:
            logger.warning(
                "Tentacle %s: failed to handle block action", self.tag, exc_info=True
            )

    async def secrete(self, seg: ImageSegment) -> None:
        apath = anyio.Path(seg.data.path)
        if not await apath.exists():
            logger.warning("Tentacle %s: image file not found: %s", self.tag, apath)
            return
        try:
            image_data = await apath.read_bytes()
            url = await self.ink.upload_media(image_data)
            if url:
                seg.data.url = url
        except Exception:
            logger.warning(
                "Tentacle %s: failed to upload image", self.tag, exc_info=True
            )

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        try:
            url = seg.data.file
            if not url:
                return
            result = await self.ink.download_media(url)
            if not result:
                return
            data, filename = result
            await anyio.Path(save_dir).mkdir(parents=True, exist_ok=True)
            ext = guess_image_ext("", filename)
            file_path = save_dir / f"{uuid.uuid4().hex}{ext}"
            if data:
                await anyio.Path(file_path).write_bytes(data)
            seg.data.file = str(file_path.resolve())
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.tag, exc_info=True
            )

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        first_ts: str | None = None
        thread_ts = reply_to if reply_in_thread else None

        for msg in messages:
            blocks = msg.metadata.get("blocks") if msg.metadata else None
            ts = await self.ink.send_message(
                chat_id,
                text=msg.content,
                blocks=blocks,
                thread_ts=thread_ts,
            )
            if first_ts is None:
                first_ts = ts
        return first_ts
