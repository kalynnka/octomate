from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    CLAUDE_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import claude_logfire
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate

    # The injected instance only needs its type here.
    from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer

logger = logging.getLogger(__name__)


class ClaudeHookIngest:
    """Live human-ledger ingest for a native Claude Code session, and the lifecycle that
    starts and finalizes its transcript tailer.

    Each turn's prompt (`UserPromptSubmit`) and answer (`Stop.last_assistant_message`)
    are written as inbound/outbound `ThreadMessage`s — a complete, lossless chat log,
    visible the moment the session runs — and sketched as the turn's provisional
    `ExternalAgentRun`, so a turn in flight is a whole conversation → run → messages
    chain and not a chat log with no model history to hang from. The events carry no
    thinking, tools or usage, so the full timeline comes from the
    `ClaudeTranscriptTailer` — fed only by the stream (`octomate claude tail`): the
    server never opens a transcript, and a hook's `transcript_path` is recorded
    context, never something to follow. `Stop` and `SessionEnd` relay the stream's
    per-turn and final drains.

    Ledger rows and the run alike are keyed by `prompt_id` — `platform_message_id` on
    the rows, the run's own id — the stable per-turn key both tiers write under, so the
    tailer's rebuilt run and the hooks' sketch are always the same run.
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
        # Sessions the tentacle is driving right now, counted by how many runs hold
        # each (`driving`) — a follow-up run supersedes a live one and the two overlap
        # while the first unwinds, so the claim outlives whichever ends first.
        self.driven: Counter[str] = Counter()

    @contextmanager
    def driving(self, session_id: str) -> Generator[None]:
        """Claim a session as one Octomate drives itself, for the length of the run.

        Its hooks fire like any other session's — an operator's settings are theirs to
        keep, and this pipe does not reach around them — but the tentacle records the
        run as it drives it, so ingesting the hooks would write that conversation a
        second time.

        The claim brackets the run rather than tracking its events: taken before the CLI
        exists, so no hook can arrive unclaimed, and dropped only once the client's
        teardown has waited out the process, by which point every hook the session can
        fire — `SessionEnd` last, on its way out — already has.
        """
        self.driven[session_id] += 1
        try:
            yield
        finally:
            self.driven[session_id] -= 1
            if self.driven[session_id] <= 0:
                del self.driven[session_id]

    async def handle(self, event: ClaudeHookInput, sender: UserProfile) -> None:
        """`sender` is the verified bearer's own profile (the route's
        `hook_sender` dependency) — the person every ledger row this event
        writes is attributed to."""
        if event.session_id in self.driven:
            # A subagent's events carry the *parent's* session id, so a driven
            # session's subagents are suppressed by the same claim.
            return
        match event.hook_event_name:
            case "SubagentStart":
                await self.on_subagent_start(event)
            case "SubagentStop":
                await self.on_subagent_stop(event)
            case _ if event.agent_id is not None:
                # Any other event carrying agent_id fired *inside* a subagent (its
                # own Stop, a PreToolUse, …) and is never a parent-turn event.
                # Unguarded, a subagent's Stop would write the parent ledger and
                # sketch the parent run with the child's answer.
                return
            case "UserPromptSubmit":
                await self.on_user_prompt_submit(event, sender)
            case "Stop":
                await self.on_stop(event, sender)
            case "SessionEnd":
                await self.on_session_end(event)

    @claude_logfire.instrument(
        "claude.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(
        self, event: ClaudeHookInput, sender: UserProfile
    ) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            if event.prompt:
                await self.record_prompt(event, event.prompt, sender)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s asked", event.session_id, event.prompt_id
                )

    @claude_logfire.instrument(
        "claude.hook Stop [{event.session_id}]", extract_args=["event"]
    )
    async def on_stop(self, event: ClaudeHookInput, sender: UserProfile) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message, sender)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s answered", event.session_id, event.prompt_id
                )
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
            if event.prompt_id and event.prompt:
                await self.sketch_subagent_run(event)
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

    async def sketch_subagent_run(self, event: ClaudeHookInput) -> None:
        """The child's provisional run, from what the hook alone sees: its opening
        prompt. `<agentId>:<promptId>` is the id the tailer commits under too, so the
        full timeline replaces this sketch as the same run. Dated now — a hook carries
        no clock, and an undated run sorts ahead of the history it belongs at the end
        of. Skipped when the event carries no `prompt_id` (the field is undocumented
        for subagent events); the tailer covers the turn from the file either way."""
        # Both guarded by the caller.
        assert event.agent_id
        assert event.prompt_id
        thread = await self.session_thread(event)
        parent = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        child = await self.octomate.conversations.ensure(
            thread.id,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            subagent_id=event.agent_id,
            parent_conversation_id=parent.id,
        )
        await self.octomate.conversations.record_external_run(
            child,
            run_id=f"{event.agent_id}:{event.prompt_id}",
            messages=[
                ModelRequest(
                    parts=[UserPromptPart(content=event.prompt or "")],
                    timestamp=datetime.now(UTC),
                )
            ],
            name=CLAUDE_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            external_session_id=event.agent_id,
            parent_run_id=event.prompt_id,
        )

    async def start_session(self, event: ClaudeHookInput) -> None:
        """Ensure the session's skeleton (thread + conversation), under the session
        lock so the write can't race the hooks' first-sighting create. The transcript
        itself arrives only through the stream; nothing here follows the path a hook
        claims."""
        thread = await self.session_thread(event)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )

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
        holder = self.octomate.projects.resolve(Path(event.cwd)) if event.cwd else None
        project = self.octomate.projects.get(holder) if holder is not None else None
        return await self.octomate.thread_manager.ensure(
            ThreadKey(
                channel_tentacle_id=CLAUDE_NATIVE_ID,
                chat_type="thread",
                chat_id=event.session_id,
                channel_thread_id=None,
            ),
            project=project,
        )

    async def record_prompt(
        self, event: ClaudeHookInput, prompt: str, sender: UserProfile
    ) -> None:
        thread = await self.session_thread(event)
        # A hook can fire more than once (retries, a repeated `Stop`); the per-turn
        # prompt_id + direction dedups so a re-fire is a no-op.
        if event.prompt_id and await self.octomate.thread_manager.find_message(
            thread.id, event.prompt_id, "inbound"
        ):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CLAUDE_NATIVE_ID,
                message_id=event.prompt_id or "",
                chat_id=event.session_id,
                chat_type="thread",
                user_id=sender.channel_user_id,
                sender=sender,
                segments=[TextSegment(data={"text": prompt})],
            )
        )

    async def sketch_run(self, event: ClaudeHookInput) -> None:
        """Write the turn's provisional run — the model timeline as the hooks alone see
        it: the prompt, then the answer once `Stop` knows it.

        A run is what joins a conversation to its messages, and the transcript's real one
        only lands when the turn closes (the next prompt, or `SessionEnd`) — so without
        this a turn in flight would have a conversation and a chat log but no run to hang
        a model history from, and nothing to reuse until it ended. The tailer replaces
        this sketch wholesale with the full timeline at close; `prompt_id` is the run id
        both write under, so they are the same run throughout.

        Both hooks route here, and both read the prompt back off the inbound ledger row:
        `Stop` carries only `last_assistant_message`, never the prompt it answers.
        """
        if not event.prompt_id:
            return  # no per-turn key: nothing to write a run under
        thread = await self.session_thread(event)
        prompt = await self.octomate.thread_manager.find_message(
            thread.id, event.prompt_id, "inbound"
        )
        if prompt is None or prompt.message_text is None:
            # The prompt hook never landed (Octomate came up mid-turn). Leave the turn
            # to the tailer, which rebuilds it from the transcript either way.
            return
        messages: list[PydanticModelMessage] = [
            # Dated by the prompt's own ledger row — its conversation clock, not the
            # moment it was written. `started_at` is read off the first
            # message, and both `Conversation.runs` and `.messages` order on it, so an
            # undated run sorts ahead of the whole history it belongs at the end of —
            # `ModelRequest.timestamp` defaults to None, unlike `ModelResponse`'s.
            ModelRequest(
                parts=[UserPromptPart(content=prompt.message_text)],
                timestamp=prompt.happened_at,
            )
        ]
        if event.last_assistant_message:
            messages.append(
                ModelResponse(parts=[TextPart(content=event.last_assistant_message)])
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        await self.octomate.conversations.record_external_run(
            conversation,
            run_id=event.prompt_id,
            messages=messages,
            name=CLAUDE_NATIVE_ID,
            cwd=Path(event.cwd) if event.cwd else None,
            external_session_id=event.session_id,
        )

    async def record_answer(
        self, event: ClaudeHookInput, answer: str, sender: UserProfile
    ) -> None:
        thread = await self.session_thread(event)
        if event.prompt_id and await self.octomate.thread_manager.find_message(
            thread.id, event.prompt_id, "outbound"
        ):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            sender=sender,
            platform_message_id=event.prompt_id or "",
        )
