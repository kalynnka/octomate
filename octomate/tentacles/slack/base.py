from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from octomate.tentacles.base import (
    ChannelTentacle,
    PlatformMessage,
    SendTarget,
    StreamSink,
)
from octomate.tentacles.feelers import Feelers
from octomate.tentacles.slack.chromo import SlackChromo, _md_to_mrkdwn
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
    from octomate.transmuters.interactions import Confirmation

logger = logging.getLogger(__name__)

THINKING_PREVIEW_LEN = 120  # chars of collapsed thinking preview shown inline
SLACK_SECTION_LIMIT = 3000  # max mrkdwn chars per Slack section block

IGNORED_SUBTYPES = frozenset(
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


def _resolution_blocks(
    action: Confirmation,
    status_emoji: str,
    status_text: str,
    clicker_id: str,
    extra_note: str = "",
) -> list[dict]:
    """Build rich post-resolution blocks retaining tool call info."""
    title = action.title or action.tool_name
    args_json = json.dumps(action.args, ensure_ascii=False, indent=2)
    if len(args_json) > 2000:
        args_json = args_json[:2000] + "\n… (truncated)"
    text = (
        f"{status_emoji} *{title}* — {status_text}\n"
        f"*Arguments:*\n```{args_json}```\n"
        f"*By:* <@{clicker_id}>"
    )
    if extra_note:
        text += f"\n{extra_note}"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


class SlackStreamSink(StreamSink):
    """Streams text into a single Slack message via post + update.

    For assistant threads, also drives the typing-status indicator.
    """

    ink: SlackInk
    channel: str
    thread_ts: str | None
    buffer: str
    message_ts: str | None
    last_update: float
    update_interval: float
    thinking_store: dict[str, str]

    def __init__(
        self,
        ink: SlackInk,
        channel: str,
        thread_ts: str | None = None,
        update_interval: float = 0.3,
        thinking_store: dict[str, str] | None = None,
    ) -> None:
        self.ink = ink
        self.channel = channel
        self.thread_ts = thread_ts
        self.buffer = ""
        self.message_ts = None
        self.last_update = 0.0
        self.update_interval = update_interval
        self.thinking_store = thinking_store if thinking_store is not None else {}

    async def set_status(self, status: str) -> None:
        if self.thread_ts:
            await self.ink.set_assistant_status(self.channel, self.thread_ts, status)

    async def append(self, text: str) -> None:
        self.buffer += text
        now = time.monotonic()
        if now - self.last_update >= self.update_interval:
            await self._push()

    async def flush(self) -> None:
        if self.buffer:
            await self._push()
        self.buffer = ""
        self.message_ts = None

    async def _push(self) -> None:
        if not self.buffer:
            return
        content = _md_to_mrkdwn(self.buffer)
        if self.message_ts is None:
            self.message_ts = await self.ink.send_message(
                self.channel, text=content, thread_ts=self.thread_ts
            )
        else:
            await self.ink.update_message(self.channel, self.message_ts, text=content)
        self.last_update = time.monotonic()

    async def post_thinking_block(self, text: str) -> None:
        """Post a collapsed 🧠 thinking message with a 'Show thinking' button.

        The full thinking text is stored in *thinking_store* (shared with the
        parent SlackTentacle).  When the user clicks the button the block action
        handler expands the message in-place, then removes the entry from the
        store to avoid unbounded growth.
        """
        if not text:
            return
        # Flush any buffered text first so this thinking message appears
        # chronologically *after* whatever was already streaming.
        await self.flush()

        thinking_id = uuid.uuid4().hex
        self.thinking_store[thinking_id] = text

        # Build a one-line preview: collapse whitespace, trim to limit.
        flat = " ".join(text.split())
        preview = flat[:THINKING_PREVIEW_LEN]
        if len(flat) > THINKING_PREVIEW_LEN:
            preview += "…"

        blocks: list[dict] = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🧠 *Thinking*  ·  _{preview}_",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "▶  Show thinking",
                            "emoji": True,
                        },
                        "action_id": f"thinking_show_{thinking_id}",
                        "value": json.dumps(
                            {
                                "action": "show_thinking",
                                "thinking_id": thinking_id,
                            }
                        ),
                    }
                ],
            },
        ]
        await self.ink.send_message(
            self.channel,
            text="🧠 Thinking…  (click ▶ to expand)",
            blocks=blocks,
            thread_ts=self.thread_ts,
        )


class SlackTentacle(ChannelTentacle):
    app: AsyncApp
    handler: AsyncSocketModeHandler | None
    ink: SlackInk
    app_token: str
    assistant_threads: set[str]
    dm_channels: dict[str, str]
    thinking_store: dict[str, str]

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        *,
        bot_token: SecretStr,
        app_token: SecretStr,
        flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        self.ink = SlackInk(bot_token)
        self.chromo = SlackChromo()
        self.assistant_threads = set()
        self.dm_channels = {}
        self.thinking_store = {}

        self.app = AsyncApp(token=bot_token.get_secret_value())
        self.app.event("message")(self.on_message)
        self.app.event("assistant_thread_started")(self.on_assistant_thread_started)
        self.app.event("assistant_thread_context_changed")(self._ack_noop)
        self.app.action(re.compile(".*"))(self.on_block_action)

        self.app_token = app_token.get_secret_value()
        self.handler = None

        super().__init__(tag, octopus, flick, memory, flush_delay=flush_delay)

        self.feelers = Feelers(
            confirm=SlackConfirmationFeeler(self.ink, self.interactions),
            todos=SlackTodoFeeler(self.ink, self.interactions),
            questions=SlackQuestionFeeler(self.ink, self.interactions),
        )

    async def activate(self) -> None:
        logger.info("Tentacle %s: starting Slack Socket Mode client", self.id)
        self.handler = AsyncSocketModeHandler(self.app, self.app_token)
        await self.handler.start_async()

    async def deactivate(self) -> None:
        if self.handler:
            await self.handler.close_async()

    async def on_message(self, event: dict, say) -> None:
        subtype = event.get("subtype")
        if subtype in IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self.profile.user_id:
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

                entry = self.interactions.confirmations.get(confirmation_id)
                if entry is None:
                    return
                action, _ = entry
                if action.approvers and clicker_id not in action.approvers:
                    return

                resolved = await self.interactions.resolve_confirmation(
                    confirmation_id, approved
                )
                if not resolved:
                    return

                status_emoji = ":white_check_mark:" if approved else ":x:"
                status_text = "Approved" if approved else "Denied"
                title = action.title or action.tool_name
                blocks = _resolution_blocks(action, status_emoji, status_text, clicker_id)
                if channel and message_ts:
                    await self.ink.update_message(
                        channel,
                        message_ts,
                        text=f"{title} — {status_text}",
                        blocks=blocks,
                    )

            elif action_type == "confirm_allow_session":
                confirmation_id = value.get("confirmation_id", "")

                entry = self.interactions.confirmations.get(confirmation_id)
                if entry is None:
                    return
                action, _ = entry
                if action.approvers and clicker_id not in action.approvers:
                    return

                resolved = await self.interactions.resolve_confirmation_allow_session(
                    confirmation_id
                )
                if not resolved:
                    return

                title = action.title or action.tool_name
                blocks = _resolution_blocks(
                    action,
                    ":white_check_mark:",
                    "Approved (allowed in session)",
                    clicker_id,
                    extra_note=":repeat: Future calls to this tool will be auto-approved in this session.",
                )
                if channel and message_ts:
                    await self.ink.update_message(
                        channel,
                        message_ts,
                        text=f"{title} — Allowed in session",
                        blocks=blocks,
                    )

            elif action_type == "question_answer":
                answer = value.get("answer", "")
                question_id = value.get("question_id", "")
                await self.interactions.resolve_question(
                    question_id, answer, clicker_id
                )

                entry = self.interactions.questions.get(question_id)
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

            elif action_type == "show_thinking":
                thinking_id = value.get("thinking_id", "")
                raw = self.thinking_store.pop(thinking_id, None)
                if not raw:
                    return  # already expanded or entry expired

                display = _md_to_mrkdwn(raw)

                # Split the (potentially very long) thinking text into section
                # blocks that each respect Slack's 3 000-char mrkdwn limit.
                # Cap at 49 content blocks to stay under Slack's 50-block ceiling
                # (one slot is reserved for the context header).
                _MAX_SECTIONS = 49
                chunks: list[str] = []
                remaining = display
                while remaining:
                    chunks.append(remaining[:SLACK_SECTION_LIMIT])
                    remaining = remaining[SLACK_SECTION_LIMIT:]
                    if len(chunks) == _MAX_SECTIONS:
                        if remaining:
                            chunks[-1] = (
                                chunks[-1][: SLACK_SECTION_LIMIT - 20]
                                + "\n…_(truncated)_"
                            )
                        break

                expanded_blocks: list[dict] = [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "🧠 *Thinking*"}],
                    },
                    *[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": chunk},
                        }
                        for chunk in chunks
                    ],
                ]
                if channel and message_ts:
                    await self.ink.update_message(
                        channel,
                        message_ts,
                        text="🧠 Thinking",
                        blocks=expanded_blocks,
                    )

        except Exception:
            logger.warning(
                "Tentacle %s: failed to handle block action", self.id, exc_info=True
            )

    @asynccontextmanager
    async def open_stream(self, target: SendTarget) -> AsyncIterator[StreamSink]:  # type: ignore[override]
        channel = str(target.chat_id)
        if target.chat_type == "private":
            channel = self.dm_channels.get(channel, channel)
        thread_ts = str(target.reply_to) if target.reply_to else None
        sink = SlackStreamSink(
            ink=self.ink,
            channel=channel,
            thread_ts=thread_ts,
            thinking_store=self.thinking_store,
        )
        try:
            yield sink
        finally:
            await sink.flush()

    async def secrete(self, seg: ImageSegment) -> None:
        apath = anyio.Path(seg.data.path)
        if not await apath.exists():
            logger.warning("Tentacle %s: image file not found: %s", self.id, apath)
            return
        try:
            image_data = await apath.read_bytes()
            url = await self.ink.upload_media(image_data)
            if url:
                seg.data.url = url
        except Exception:
            logger.warning(
                "Tentacle %s: failed to upload image", self.id, exc_info=True
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
        first_ts: str | None = None
        # slack should always reply in thread,
        # the only difference is keep in the main thread or create a new sub-thread
        for msg in messages:
            blocks = msg.metadata.get("blocks") if msg.metadata else None
            ts = await self.ink.send_message(
                chat_id,
                text=msg.content,
                blocks=blocks,
                thread_ts=reply_to,
            )
            if first_ts is None:
                first_ts = ts
        return first_ts
