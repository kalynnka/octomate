from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import logfire

from octomate.schemas.conversation import UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadKey, ThreadMessageDirection
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate

    # Runtime dependency runs the other way (the tailer imports this module's
    # CLAUDE_NATIVE_ID); the injected instance only needs its type here.
    from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer

logger = logging.getLogger(__name__)

# Synthetic channel/agent id marking a thread as ingested from a native Claude client
# rather than driven by a live tentacle. No channel is registered under it: an ingested
# thread is recorded, never dispatched to.
CLAUDE_NATIVE_ID = "claude-native"

# A native session carries no platform identity for whoever is typing.
NATIVE_USER = UserProfile(user_id="native", name="native")


class ClaudeHookIngest:
    """Live human-ledger ingest for a native Claude Code session, and the lifecycle that
    starts and finalizes its transcript tailer.

    Each turn's prompt (`UserPromptSubmit`) and answer (`Stop.last_assistant_message`)
    are written as inbound/outbound `ThreadMessage`s — a complete, lossless chat log,
    visible the moment the session runs. The events carry no thinking or usage, so the
    full model timeline comes from the `ClaudeTranscriptTailer`, which this starts on the
    first prompt (`SessionStart` is not delivered to http hooks) and finalizes at
    `SessionEnd`. No transcript is read here.

    Both ledger rows are stamped `platform_message_id = prompt_id`, the stable per-turn
    key the tailer binds its rebuilt runs against.
    """

    def __init__(
        self,
        octomate: Octomate,
        tailer: ClaudeTranscriptTailer,
        locks: SessionLocks | None = None,
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        # Serialize a session's events so the existence check and the write can't race
        # (Claude fires the next event without waiting for our commit). Shared with the
        # tailer so its run/ledger commits serialize against these writes too; the
        # registry reclaims a session's lock on its own once no one holds it.
        self.locks = locks if locks is not None else SessionLocks()

    async def handle(self, event: ClaudeHookInput) -> None:
        match event.hook_event_name:
            case "SessionStart":
                await self.on_session_start(event)
            case "UserPromptSubmit":
                await self.on_user_prompt_submit(event)
            case "Stop":
                await self.on_stop(event)
            case "SessionEnd":
                await self.on_session_end(event)

    @logfire.instrument(
        "claude.hook SessionStart [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_start(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)

    @logfire.instrument(
        "claude.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(self, event: ClaudeHookInput) -> None:
        # SessionStart is not delivered to http hooks, so the first prompt starts the
        # tailer if it is not already following (self-heal).
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            if event.prompt:
                await self.record_prompt(event, event.prompt)

    @logfire.instrument("claude.hook Stop [{event.session_id}]", extract_args=["event"])
    async def on_stop(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message)

    @logfire.instrument(
        "claude.hook SessionEnd [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_end(self, event: ClaudeHookInput) -> None:
        # Finalize outside any lock: it awaits the follow loop's own last commit, which
        # takes the session lock — holding it here would deadlock.
        await self.tailer.finalize(event.session_id)

    async def start_session(self, event: ClaudeHookInput) -> None:
        """Ensure the session's skeleton (thread + conversation) and start its transcript
        tailer, once per session. Ensuring the skeleton here — under the session lock,
        before the follow loop runs — is what keeps the tailer's own `ensure` a cache hit
        rather than a write that could race the hooks' first-sighting create.

        Requires the transcript directory to exist (the tailer's watch needs it); a hook
        carrying no usable path leaves the tailer unstarted, and the ledger still writes.
        """
        path = event.transcript_path
        if self.tailer.is_following(event.session_id) or path is None:
            return
        if not path.parent.is_dir():
            return
        thread = await self.session_thread(event.session_id)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        self.tailer.start(event.session_id, path)
        logfire.info(
            "claude.tailer started for session {session_id}",
            session_id=event.session_id,
            transcript_path=str(path),
        )

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
