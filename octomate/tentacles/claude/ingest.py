"""Hook-driven ingest of a native Claude Code session.

Hooks establish session and run identities and signal transcript boundaries.
Only the transcript tail writes messages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from octomate.schemas.thread import (
    CLAUDE_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.telemetry import claude_logfire
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.locks import SessionLocks

if TYPE_CHECKING:
    from octomate.managers.conversation import ConversationManager
    from octomate.managers.project import ProjectManager
    from octomate.managers.thread import ThreadManager

    # The injected instance only needs its type here.
    from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer

logger = logging.getLogger(__name__)


class ClaudeHookIngest:
    """Establish native session/run identities and relay transcript boundaries.

    Hooks may arrive before or after their transcript. They ensure empty runs
    under the same native turn keys, but never write messages or replace a run's
    contents. The tail alone creates model messages and their ledger bindings.
    Its authenticated stream supplies the sender. A hook's transcript path is
    client context, never a server-local file to open.
    """

    conversations: ConversationManager
    projects: ProjectManager
    threads: ThreadManager
    tailer: ClaudeTranscriptTailer
    locks: SessionLocks

    def __init__(
        self,
        tailer: ClaudeTranscriptTailer,
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
        # Serialize a session's events so the existence check and the write can't race
        # (Claude fires the next event without waiting for our commit). Shared with the
        # tailer so its run/ledger commits serialize against these writes too; the
        # registry reclaims a session's lock on its own once no one holds it.
        self.locks = locks if locks is not None else SessionLocks()

    async def handle(self, event: ClaudeHookInput) -> None:
        """Handle an authenticated hook; message attribution belongs to the stream."""
        if (
            event.agent_id is None
            and event.prompt_id
            and await self.conversations.driven_run(
                CLAUDE_NATIVE_ID, event.session_id, event.prompt_id
            )
            is not None
        ):
            if event.hook_event_name == "Stop":
                await self.tailer.stop_turn(event.session_id, event.prompt_id)
            return
        match event.hook_event_name:
            case "SubagentStart":
                await self.on_subagent_start(event)
            case "SubagentStop":
                await self.on_subagent_stop(event)
            case _ if event.agent_id is not None:
                # Any other event carrying agent_id fired *inside* a subagent (its
                # own Stop, a PreToolUse, …) and is never a parent-turn event.
                # A child's Stop must never drain the parent turn.
                return
            case "UserPromptSubmit":
                await self.on_user_prompt_submit(event)
            case "Stop":
                await self.on_stop(event)
            case "SessionEnd":
                await self.on_session_end(event)

    @claude_logfire.instrument(
        "claude.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            await self.ensure_run(event)

    @claude_logfire.instrument(
        "claude.hook Stop [{event.session_id}]", extract_args=["event"]
    )
    async def on_stop(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            await self.ensure_run(event)
        # The turn is over and its transcript flushed: let the tail commit it now
        # rather than at the next prompt — a remote one drains out and exits. Outside
        # the lock, because a local close takes the same session lock to commit.
        await self.tailer.stop_turn(event.session_id, event.prompt_id)

    @claude_logfire.instrument(
        "claude.hook SessionEnd [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_end(self, event: ClaudeHookInput) -> None:
        logger.info("session %s: ended", event.session_id)
        # The thread first: for a session whose earlier hooks never arrived this is
        # the last event carrying a cwd, and a thread's project is frozen at creation
        # — a backfill tail attaching later would create it unfiled. Already-created
        # sessions resolve to their existing row.
        await self.session_thread(event)
        # Finalize outside any lock: it awaits the stream's drain commits, which take
        # the session lock — holding it here would deadlock.
        await self.tailer.finalize(event.session_id)

    @claude_logfire.instrument(
        "claude.hook SubagentStart [{event.session_id}/{event.agent_id}]",
        extract_args=["event"],
    )
    async def on_subagent_start(self, event: ClaudeHookInput) -> None:
        if not event.agent_id:
            return
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            if event.prompt_id:
                await self.ensure_subagent_run(event)
        logger.info("session %s: subagent %s started", event.session_id, event.agent_id)

    @claude_logfire.instrument(
        "claude.hook SubagentStop [{event.session_id}/{event.agent_id}]",
        extract_args=["event"],
    )
    async def on_subagent_stop(self, event: ClaudeHookInput) -> None:
        if not event.agent_id:
            return
        # The subagent has written its last line by the time this synchronous hook
        # fires — and a Claude child's own file never closes its last turn — so wait
        # for the streamed answer and commit the open turn now. A session no stream
        # covers has nothing to settle.
        await self.tailer.finish_subagent(
            event.session_id,
            event.agent_id,
            final_answer=event.last_assistant_message,
        )
        logger.info("session %s: subagent %s stopped", event.session_id, event.agent_id)

    async def ensure_subagent_run(self, event: ClaudeHookInput) -> None:
        """Ensure the child's empty run without copying its hook prompt."""
        # Both guarded by the caller.
        assert event.agent_id
        assert event.prompt_id
        thread = await self.session_thread(event)
        parent = await self.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        child = await self.conversations.ensure(
            thread.id,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            subagent_id=event.agent_id,
            parent_conversation_id=parent.id,
        )
        await self.conversations.record_external_run(
            child,
            run_id=f"{event.agent_id}:{event.prompt_id}",
            name=CLAUDE_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            native_session_id=event.agent_id,
            parent_run_id=event.prompt_id,
        )

    async def start_session(self, event: ClaudeHookInput) -> None:
        """Ensure the session's skeleton (thread + conversation), under the session
        lock so the write can't race the hooks' first-sighting create. The transcript
        itself arrives only through the stream; nothing here follows the path a hook
        claims."""
        thread = await self.session_thread(event)
        await self.conversations.ensure(thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID)

    async def session_thread(self, event: ClaudeHookInput) -> Thread:
        """This session's thread, filed under the project already holding the directory
        it is running in — and under none when no project holds it.

        The session's own directory is what says which project this is, not the
        transcript path, which lives in Claude's tree whatever the code does. Nothing is
        registered from here: Claude's own `projects` are a context store keyed by
        directory, not a claim that a tree is worked in, so a Claude session is not
        evidence of a project the way a Codex workspace is. The directory is recorded on
        the run regardless, which is what a later promotion would read.

        The project lands only when the thread is created, so resolving it on every
        later hook costs a path comparison and rewrites nothing.
        """
        # Only a cwd the hook actually carried: `Path("")` is the process's own
        # directory, which would attribute every session to whatever project
        # Octomate itself was started in.
        holder = self.projects.resolve(Path(event.cwd)) if event.cwd else None
        project = self.projects.get(holder) if holder is not None else None
        return await self.threads.ensure(
            ThreadKey(
                channel_tentacle_id=CLAUDE_NATIVE_ID,
                chat_type="thread",
                chat_id=event.session_id,
                channel_thread_id=None,
            ),
            project=project,
        )

    async def ensure_run(self, event: ClaudeHookInput) -> None:
        """Reserve the turn's foreign-key target; repeated or late hooks change nothing."""
        if not event.prompt_id:
            return
        thread = await self.session_thread(event)
        conversation = await self.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        await self.conversations.record_external_run(
            conversation,
            run_id=event.prompt_id,
            name=CLAUDE_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            native_session_id=event.session_id,
        )
