"""Hook-driven ingest of a native Codex session.

Hooks establish session and run identities and signal transcript boundaries.
Only the rollout tail writes messages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from octomate.schemas.thread import (
    CODEX_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.telemetry import codex_logfire
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.locks import SessionLocks

if TYPE_CHECKING:
    from octomate.managers.conversation import ConversationManager
    from octomate.managers.project import ProjectManager
    from octomate.managers.thread import ThreadManager
    from octomate.tentacles.codex.tailer import CodexTranscriptTailer

logger = logging.getLogger(__name__)


class CodexHookIngest:
    """Reserve native session/run identities and request a drain when a turn stops.

    Hooks never write message history. The authenticated stream owns attribution
    and the rollout is the sole source of prompts, replies and tool activity.
    """

    conversations: ConversationManager
    projects: ProjectManager
    threads: ThreadManager
    tailer: CodexTranscriptTailer
    locks: SessionLocks

    def __init__(
        self,
        tailer: CodexTranscriptTailer,
        locks: SessionLocks | None = None,
        *,
        conversations: ConversationManager,
        projects: ProjectManager,
        threads: ThreadManager,
    ) -> None:
        self.conversations = conversations
        self.projects = projects
        self.threads = threads
        self.tailer = tailer
        self.locks = locks if locks is not None else SessionLocks()

    @codex_logfire.instrument(
        "codex.hook {event.hook_event_name} [{event.session_id}]",
        extract_args=["event"],
    )
    async def handle(self, event: CodexHookInput) -> None:
        """Handle an authenticated hook; the stream owns message attribution."""
        if (
            event.agent_id is None
            and event.turn_id
            and await self.conversations.driven_run(
                CODEX_NATIVE_ID, event.session_id, event.turn_id
            )
            is not None
        ):
            if event.hook_event_name == "Stop":
                await self.tailer.stop_turn(event.session_id, event.turn_id)
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
        if event.session_name and event.hook_event_name in {
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
        }:
            async with self.locks.hold(event.session_id):
                thread = await self.session_thread(event)
                conversation = await self.conversations.ensure(
                    thread.id, agent_tentacle_id=CODEX_NATIVE_ID
                )
                await self.conversations.set_name(conversation, event.session_name)
                await self.threads.rename(thread, event.session_name)
        if event.hook_event_name == "Stop":
            await self.on_stop(event)
            return
        if event.hook_event_name in {"SessionStart", "UserPromptSubmit"}:
            async with self.locks.hold(event.session_id):
                await self.start_session(event)
                if event.hook_event_name == "UserPromptSubmit":
                    await self.reserve_run(event)
                    logger.info(
                        "session %s: turn %s asked",
                        event.session_id,
                        event.turn_id,
                    )

    async def on_stop(self, event: CodexHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.reserve_run(event)
        # Return the hook so Codex can flush its terminal line; that line settles
        # the turn and asks the connected tail to drain.
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
        await self.conversations.ensure(thread.id, agent_tentacle_id=CODEX_NATIVE_ID)

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
        holder = self.projects.resolve(Path(event.cwd)) if event.cwd else None
        project = self.projects.get(holder) if holder is not None else None
        return await self.threads.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", event.session_id),
            project=project,
        )

    async def reserve_run(self, event: CodexHookInput) -> None:
        """Keep the native identity available without creating any messages."""
        if not event.turn_id:
            return
        thread = await self.session_thread(event)
        conversation = await self.conversations.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )
        await self.conversations.record_external_run(
            conversation,
            run_id=event.turn_id,
            name=CODEX_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            native_session_id=event.session_id,
        )
