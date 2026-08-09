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
    ThreadMessage,
    ThreadMessageDirection,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import claude_logfire
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.transcript import CLAUDE_PROJECTS_DIRS
from octomate.tentacles.agents.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate

    # Runtime dependency runs the other way (the tailer imports this module's
    # NATIVE_USER); the injected instance only needs its type here.
    from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer

logger = logging.getLogger(__name__)

# A native session carries no platform identity for whoever is typing.
NATIVE_USER = UserProfile(channel_user_id="native", name="native")


class ClaudeHookIngest:
    """Live human-ledger ingest for a native Claude Code session, and the lifecycle that
    starts and finalizes its transcript tailer.

    Each turn's prompt (`UserPromptSubmit`) and answer (`Stop.last_assistant_message`)
    are written as inbound/outbound `ThreadMessage`s — a complete, lossless chat log,
    visible the moment the session runs — and sketched as the turn's provisional
    `ExternalAgentRun`, so a turn in flight is a whole conversation → run → messages
    chain and not a chat log with no model history to hang from. The events carry no
    thinking, tools or usage, so the full timeline comes from the
    `ClaudeTranscriptTailer`, which this starts on the first prompt (`SessionStart` is
    not delivered to http hooks) and finalizes at `SessionEnd`; it replaces each sketch
    as its turn closes. No transcript is read here.

    Ledger rows and the run alike are keyed by `prompt_id` — `platform_message_id` on
    the rows, the run's own id — the stable per-turn key both tiers write under, so the
    tailer's rebuilt run and the hooks' sketch are always the same run.
    """

    def __init__(
        self,
        octomate: Octomate,
        tailer: ClaudeTranscriptTailer,
        locks: SessionLocks | None = None,
        extra_transcript_roots: tuple[Path, ...] = (),
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        # The trees a hook may name a transcript in: Claude's own, plus any an operator
        # adds. A union rather than a replacement — the documented location keeps working
        # whatever else is configured, so naming an extra root can never be the reason a
        # session silently stops being ingested.
        #
        # Resolved once: the test is `is_relative_to`, which is lexical, so both sides
        # must be resolved for `..` to mean anything.
        self.transcript_roots = tuple(
            root.resolve() for root in (*CLAUDE_PROJECTS_DIRS, *extra_transcript_roots)
        )
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

    async def handle(self, event: ClaudeHookInput) -> None:
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
                await self.on_user_prompt_submit(event)
            case "Stop":
                await self.on_stop(event)
            case "SessionEnd":
                await self.on_session_end(event)

    @claude_logfire.instrument(
        "claude.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(self, event: ClaudeHookInput) -> None:
        # SessionStart is not delivered to http hooks, so the first prompt starts the
        # tailer if it is not already following (self-heal).
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            if event.prompt:
                await self.record_prompt(event, event.prompt)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s asked", event.session_id, event.prompt_id
                )

    @claude_logfire.instrument(
        "claude.hook Stop [{event.session_id}]", extract_args=["event"]
    )
    async def on_stop(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s answered", event.session_id, event.prompt_id
                )

    @claude_logfire.instrument(
        "claude.hook SessionEnd [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_end(self, event: ClaudeHookInput) -> None:
        logger.info("session %s: ended", event.session_id)
        # Finalize outside any lock: it awaits the follow loop's own last commit, which
        # takes the session lock — holding it here would deadlock.
        await self.tailer.finalize(event.session_id, event.transcript_path)

    @claude_logfire.instrument(
        "claude.hook SubagentStart [{event.session_id}/{event.agent_id}]",
        extract_args=["event"],
    )
    async def on_subagent_start(self, event: ClaudeHookInput) -> None:
        if not event.agent_id:
            return
        async with self.locks.hold(event.session_id):
            # A subagent starting proves its session is live: start the session tail
            # if nothing is following (Octomate came up mid-session), exactly as the
            # first prompt does.
            await self.start_session(event, self.session_transcript_path(event))
            if event.prompt_id and event.prompt:
                await self.sketch_subagent_run(event)
        # Outside the lock, like every tailer wait: give the child its first pump now
        # instead of waiting for the parent's next event or the 60s poll tick.
        await self.tailer.poke_subagent(event.session_id, event.agent_id)
        logger.info("session %s: subagent %s started", event.session_id, event.agent_id)

    @claude_logfire.instrument(
        "claude.hook SubagentStop [{event.session_id}/{event.agent_id}]",
        extract_args=["event"],
    )
    async def on_subagent_stop(self, event: ClaudeHookInput) -> None:
        if not event.agent_id:
            return
        if self.tailer.is_following(event.session_id):
            # The subagent has written its last line by the time this synchronous
            # hook fires — the same trust `SessionEnd` extends to the parent's
            # trailing turn — so drain and commit its open turn now.
            await self.tailer.finish_subagent(
                event.session_id,
                event.agent_id,
                final_answer=event.last_assistant_message,
            )
        else:
            # Nothing following: rebuild from disk. `recover` walks the subagents
            # directory too, so the child this hook announced is included.
            await self.tailer.recover(
                event.session_id, self.session_transcript_path(event)
            )
        logger.info("session %s: subagent %s stopped", event.session_id, event.agent_id)

    def session_transcript_path(self, event: ClaudeHookInput) -> Path | None:
        """The *session* transcript a subagent event names. Whether its
        `transcript_path` is the child's own file or the parent's is undocumented, so
        accept both: a child path (`…/<session-id>/subagents/agent-<id>.jsonl`)
        derives its session's (`…/<session-id>.jsonl`); anything else is already it."""
        path = event.transcript_path
        if path is None or event.agent_id is None:
            return path
        if path.name == f"agent-{event.agent_id}.jsonl":
            return path.parent.parent.with_suffix(".jsonl")
        return path

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
            cwd=event.cwd,
            external_session_id=event.agent_id,
            parent_run_id=event.prompt_id,
        )

    async def start_session(
        self, event: ClaudeHookInput, transcript_path: Path | None = None
    ) -> None:
        """Ensure the session's skeleton (thread + conversation) and start its transcript
        tailer, once per session. Ensuring the skeleton here — under the session lock,
        before the follow loop runs — is what keeps the tailer's own `ensure` a cache hit
        rather than a write that could race the hooks' first-sighting create.

        Requires the transcript directory to exist (the tailer's watch needs it) and the
        path to name a transcript in Claude's own tree; a hook carrying no usable path
        leaves the tailer unstarted, and the ledger still writes. `transcript_path`
        overrides the event's own — a subagent event may name the child's file, and the
        session tail must follow the session's.
        """
        claimed = transcript_path or event.transcript_path
        if self.tailer.is_following(event.session_id) or claimed is None:
            return
        # Resolved before the root test: `is_relative_to` is lexical, so an unresolved
        # `.../projects/../../elsewhere` would pass it.
        path = claimed.resolve()
        if not any(path.is_relative_to(root) for root in self.transcript_roots):
            # The path is the caller's claim, and following it means reading whatever it
            # names into this session's history. Only Claude's own tree is in scope.
            logger.warning(
                "session %s: refusing transcript outside %s: %s. If Claude Code writes "
                "elsewhere here, set agents.claude.transcript_root.",
                event.session_id,
                ", ".join(str(root) for root in self.transcript_roots),
                path,
            )
            return
        if not path.parent.is_dir():
            return
        thread = await self.session_thread(event)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        self.tailer.start(event.session_id, path)
        logger.info("session %s: tailing %s", event.session_id, path)

    async def session_thread(self, event: ClaudeHookInput) -> Thread:
        """This session's thread, attributed to the project it is running in.

        The session's own directory is what says which project this is — not the
        transcript path, which lives in Claude's tree whatever the code does. A
        directory no project holds yet becomes one: Claude keeps its own workspace per
        directory, and a session running there is proof that directory is worked in.
        The project lands only when the thread is created, so resolving it on every
        later hook costs a path comparison and rewrites nothing.

        A project root is never a transcript root: this asks where the code is, and
        `transcript_roots` bounds where a transcript may be read from. Widening one
        with the other would let a project entry open what the tailer will follow.
        """
        # Only a cwd the hook actually carried: `Path("")` is the process's own
        # directory, which would attribute every session to whatever project
        # Octomate itself was started in.
        project = None
        if event.cwd:
            project = await self.octomate.projects.ensure(
                Path(event.cwd), origin="claude"
            )
        return await self.octomate.thread_manager.ensure(
            ThreadKey(
                channel_tentacle_id=CLAUDE_NATIVE_ID,
                chat_type="thread",
                chat_id=event.session_id,
                channel_thread_id=None,
            ),
            project=project,
        )

    async def record_prompt(self, event: ClaudeHookInput, prompt: str) -> None:
        thread = await self.session_thread(event)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "inbound"
        ):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CLAUDE_NATIVE_ID,
                message_id=event.prompt_id or "",
                chat_id=event.session_id,
                chat_type="thread",
                user_id=NATIVE_USER.channel_user_id,
                sender=NATIVE_USER,
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
        prompt = self.recorded_prompt(thread, event.prompt_id)
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
            cwd=event.cwd,
            external_session_id=event.session_id,
        )

    def recorded_prompt(self, thread: Thread, prompt_id: str) -> ThreadMessage | None:
        """The turn's inbound ledger row, as `record_prompt` wrote it — the prompt's text
        and the time it arrived."""
        return next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == prompt_id
                and message.direction == "inbound"
            ),
            None,
        )

    async def record_answer(self, event: ClaudeHookInput, answer: str) -> None:
        thread = await self.session_thread(event)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "outbound"
        ):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            sender=NATIVE_USER,
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
