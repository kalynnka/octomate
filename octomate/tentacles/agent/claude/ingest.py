from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from octomate.schemas.conversation import UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadKey, ThreadMessageDirection
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput

if TYPE_CHECKING:
    from octomate import Octomate

logger = logging.getLogger(__name__)

# Synthetic channel/agent id marking a thread as ingested from a native Claude client
# rather than driven by a live tentacle. No channel is registered under it: an ingested
# thread is recorded, never dispatched to.
CLAUDE_NATIVE_ID = "claude-native"

# A native session carries no platform identity for whoever is typing.
NATIVE_USER = UserProfile(user_id="native", name="native")


class ClaudeHookIngest:
    """Live human-ledger ingest for a native Claude Code session.

    Each turn's prompt (`UserPromptSubmit`) and answer (`Stop.last_assistant_message`)
    are written as inbound/outbound `ThreadMessage`s — a complete, lossless chat log,
    visible the moment the session runs. Nothing else is persisted live: model messages
    need thinking and usage the events do not carry, so the full run timeline is rebuilt
    from the transcript on restore. No transcript is read here.

    Both rows are stamped `platform_message_id = prompt_id`, the stable per-turn key
    restore binds its rebuilt runs against.
    """

    def __init__(self, octomate: Octomate) -> None:
        self.octomate = octomate
        # Serialize a session's events so the existence check and the write can't race
        # (Claude fires the next event without waiting for our commit). Dropped at
        # SessionEnd so the map does not grow unbounded.
        self.locks: dict[str, asyncio.Lock] = {}

    async def handle(self, event: ClaudeHookInput) -> None:
        lock = self.locks.setdefault(event.session_id, asyncio.Lock())
        async with lock:
            match event.hook_event_name:
                case "UserPromptSubmit":
                    if event.prompt:
                        await self.record_prompt(event, event.prompt)
                case "Stop":
                    if event.last_assistant_message:
                        await self.record_answer(event, event.last_assistant_message)
                case "SessionEnd":
                    self.locks.pop(event.session_id, None)

    async def session_thread(self, session_id: str) -> Thread:
        return await self.octomate.thread_manager.ensure(
            ThreadKey(
                channel_tentacle_id=CLAUDE_NATIVE_ID,
                chat_type="private",
                chat_id=session_id,
                thread_id="",
            )
        )

    async def record_prompt(self, event: ClaudeHookInput, prompt: str) -> None:
        thread = await self.session_thread(event.session_id)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "inbound"
        ):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CLAUDE_NATIVE_ID,
                message_id=event.prompt_id or "",
                chat_id=event.session_id,
                chat_type="private",
                user_id=NATIVE_USER.user_id,
                sender=NATIVE_USER,
                segments=[TextSegment(data={"text": prompt})],
            )
        )

    async def record_answer(self, event: ClaudeHookInput, answer: str) -> None:
        thread = await self.session_thread(event.session_id)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "outbound"
        ):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            platform_message_id=event.prompt_id or "",
        )

    def already_recorded(
        self, thread: Thread, prompt_id: str, direction: ThreadMessageDirection
    ) -> bool:
        """A hook can fire more than once (retries, a repeated `Stop`); the per-turn
        `prompt_id` + direction dedups so a re-fire is a no-op."""
        return any(
            message.platform_message_id == prompt_id and message.direction == direction
            for message in thread.messages
        )
