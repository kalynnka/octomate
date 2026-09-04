from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    CODEX_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import codex_logfire
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate
    from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer

logger = logging.getLogger(__name__)


class CodexHookIngest:
    """The hook pipe's half of native-session ingest: the immediate ledger sketch
    (prompt at `UserPromptSubmit`, answer at `Stop`) and the session skeleton. The
    transcript itself arrives only through the stream (`octomate codex tail`) — the
    server never opens a rollout, so a hook's `transcript_path` is recorded context,
    never something to follow."""

    def __init__(
        self,
        octomate: Octomate,
        tailer: CodexTranscriptTailer,
        locks: SessionLocks | None = None,
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        self.locks = locks if locks is not None else SessionLocks()
        self.driven: Counter[str] = Counter()

    @contextmanager
    def driving(self, session_id: str) -> Generator[None]:
        self.driven[session_id] += 1
        try:
            yield
        finally:
            self.driven[session_id] -= 1
            if self.driven[session_id] <= 0:
                del self.driven[session_id]

    @codex_logfire.instrument(
        "codex.hook {event.hook_event_name} [{event.session_id}]",
        extract_args=["event"],
    )
    async def handle(self, event: CodexHookInput, sender: UserProfile) -> None:
        """`sender` is the verified bearer's own profile (the route's
        `hook_sender` dependency) — the person every ledger row this event
        writes is attributed to."""
        if event.octomate_driven or event.session_id in self.driven:
            logger.debug("session %s: ignored driven Codex hook", event.session_id)
            return
        if event.hook_event_name == "SubagentStart":
            await self.on_subagent_start(event)
            return
        if event.hook_event_name == "SubagentStop":
            await self.on_subagent_stop(event)
            return
        # Child rollout ingestion owns other agent-scoped events.
        if event.agent_id is not None:
            return
        if event.hook_event_name == "Stop":
            await self.on_stop(event, sender)
            return
        if event.hook_event_name in {"SessionStart", "UserPromptSubmit"}:
            async with self.locks.hold(event.session_id):
                await self.start_session(event)
                if event.hook_event_name == "UserPromptSubmit" and event.prompt:
                    await self.record_prompt(event, event.prompt, sender)
                    await self.sketch_run(event)
                    logger.info(
                        "session %s: turn %s asked",
                        event.session_id,
                        event.turn_id,
                    )

    async def on_stop(self, event: CodexHookInput, sender: UserProfile) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message, sender)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s answered",
                    event.session_id,
                    event.turn_id,
                )
        # Stop is a turn boundary in Codex, not a session boundary. Settle after the
        # hook releases the shared lock: the rollout's task_complete line flushes a
        # beat behind the hook, and the tailer waits it out (then drains a remote
        # tail) instead of leaving the turn to the watch's next wake.
        await self.tailer.stop_turn(event.session_id, event.turn_id)

    async def on_subagent_start(self, event: CodexHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
        logger.info(
            "session %s: Codex subagent %s started",
            event.session_id,
            event.agent_id,
        )

    async def on_subagent_stop(self, event: CodexHookInput) -> None:
        # The child's turns close on their own `task_complete` lines as they stream
        # in; the launcher hook's spool is what hands the tail the child's path, so
        # the server has nothing to add here beyond the session skeleton.
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
        logger.info(
            "session %s: Codex subagent %s stopped",
            event.session_id,
            event.agent_id,
        )

    async def start_session(self, event: CodexHookInput) -> None:
        thread = await self.session_thread(event)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )

    async def session_thread(self, event: CodexHookInput) -> Thread:
        """This session's thread, filed under the project already holding the
        directory it is running in — and under none when no project holds it.

        As on the Claude side: the session's own directory says which project this
        is, the project lands only when the thread is created, and a rollout root is
        never a project root. Nothing is registered from here — every project is
        declared. Only a cwd the hook carried — `Path("")` is the process's own
        directory, which would attribute every session to whatever project Octomate
        itself was started in.
        """
        holder = self.octomate.projects.resolve(Path(event.cwd)) if event.cwd else None
        project = self.octomate.projects.get(holder) if holder is not None else None
        return await self.octomate.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", event.session_id),
            project=project,
        )

    async def record_prompt(
        self, event: CodexHookInput, prompt: str, sender: UserProfile
    ) -> None:
        thread = await self.session_thread(event)
        # A hook can fire more than once (a retry, a repeated Stop); the per-turn
        # turn_id + direction dedups so a re-fire is a no-op.
        if event.turn_id and await self.octomate.thread_manager.find_message(
            thread.id, event.turn_id, "inbound"
        ):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CODEX_NATIVE_ID,
                message_id=event.turn_id or "",
                chat_id=event.session_id,
                chat_type="thread",
                user_id=sender.channel_user_id,
                sender=sender,
                segments=[TextSegment(data={"text": prompt})],
            )
        )

    async def record_answer(
        self, event: CodexHookInput, answer: str, sender: UserProfile
    ) -> None:
        thread = await self.session_thread(event)
        if event.turn_id and await self.octomate.thread_manager.find_message(
            thread.id, event.turn_id, "outbound"
        ):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CODEX_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            sender=sender,
            platform_message_id=event.turn_id or "",
        )

    async def sketch_run(self, event: CodexHookInput) -> None:
        if not event.turn_id:
            return
        thread = await self.session_thread(event)
        prompt = await self.octomate.thread_manager.find_message(
            thread.id, event.turn_id, "inbound"
        )
        if prompt is None or prompt.message_text is None:
            return
        messages: list[ModelMessage] = [
            ModelRequest(
                parts=[UserPromptPart(prompt.message_text)],
                timestamp=prompt.happened_at,
            )
        ]
        if event.last_assistant_message:
            messages.append(
                ModelResponse(parts=[TextPart(event.last_assistant_message)])
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )
        await self.octomate.conversations.record_external_run(
            conversation,
            run_id=event.turn_id,
            messages=messages,
            name=CODEX_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            external_session_id=event.session_id,
        )
