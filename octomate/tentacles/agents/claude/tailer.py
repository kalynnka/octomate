from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from watchfiles import awatch

from octomate.capabilities.harness.events import StreamEvents
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    Thread,
    ThreadKey,
    ThreadMessage,
    ThreadMessageDirection,
)
from octomate.telemetry import claude_logfire
from octomate.tentacles.agents.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, NATIVE_USER
from octomate.tentacles.agents.claude.transcript import (
    TranscriptAssistantLine,
    TranscriptLine,
    TranscriptUserLine,
    locate_transcript,
    prompt_text,
    transcript_line_adapter,
)
from octomate.tentacles.agents.locks import SessionLocks

logger = logging.getLogger(__name__)

# The live stream is a forward-looking convenience for a future UI channel: bounded and
# drop-on-full, so a session nobody is watching never backpressures the tailer.
# Durability rests entirely on the `ExternalAgentRun` sink, never on a consumer existing.
LIVE_STREAM_BUFFER = 256

# Bound the wait for the session lock when committing a turn, so a wedged hook holder
# can't hang the follow loop (and, through `finalize`, shutdown). Timing out ends the
# loop — see `close_turn` for why a turn that can't commit must stop the tail rather
# than be skipped past.
COMMIT_LOCK_TIMEOUT = 30.0

# Reclaim a follow loop whose session went silent (crash, hooks removed — no SessionEnd
# ever arrives): if no new bytes land for this long, the loop self-finalizes (drains,
# commits the trailing turn, drops itself) instead of parking on the watch forever. The
# watch wakes every `IDLE_POLL_MS` even without a file change, to check the deadline.
IDLE_TIMEOUT = 30 * 60.0
IDLE_POLL_MS = 60_000

# A subagent's final answer line races its synchronous SubagentStop hook — measured
# live, 2 of 3 children had it land ~1-2KB after the hook fired. The event names the
# answer (`last_assistant_message`), so `finish_subagent` drains until the file yields
# it, bounded by this window, then closes with what there is.
SUBAGENT_SETTLE_TIMEOUT = 2.0
SUBAGENT_SETTLE_POLL = 0.2


def assembled(conversation: Conversation) -> set[str]:
    """The ids of the turns already built from the transcript — the ones a tail may skip.

    The byte range is the mark: a run without one is the hooks' provisional sketch of a
    turn (`ClaudeHookIngest.sketch_run`), which the tailer is precisely what replaces. It
    must never seed a committed-turn guard, or the sketch would be mistaken for the real
    timeline and the turn would never be filled in.
    """
    return {
        run.id
        for run in conversation.runs
        if isinstance(run, ExternalAgentRun) and run.end_offset is not None
    }


def stamp(messages: list[PydanticModelMessage], timestamp: datetime | None) -> None:
    """Date the messages a transcript record produced by that record's own clock — the
    accumulator stamps `now`, which is wrong for a run replayed off disk
    (`AgentRun.started_at` and ordering both read from these)."""
    if timestamp is None:
        return
    for message in messages:
        message.timestamp = timestamp


@dataclass
class OpenTurn:
    """The turn currently being assembled off the live tail — its accumulator fed line
    by line, its byte range advancing to cover the last transcript line folded in. It
    commits as an `ExternalAgentRun` when the next prompt line closes it, or at
    `finalize`."""

    prompt_id: str
    prompt_text: str  # the human's clean prompt, for creating the inbound ledger row
    source: str | None  # the transcript `entrypoint` (claude-vscode / cli / …)
    start_offset: int  # byte offset of this turn's opening prompt line
    end_offset: int  # byte offset past the last line folded in
    accumulator: ClaudeRunAccumulator
    last_line_uuid: str | None


@dataclass
class SubagentTail:
    """One subagent transcript's cursor within a session's tail. A subagent writes its
    own file (`<session>/subagents/agent-<agentId>.jsonl`), so it gets its own byte
    cursor, conversation, and open turn — pumped by the owning session's loop rather
    than a watcher of its own. Its turns frame on `promptId` change: subagent files
    carry no `promptSource`, and a resumed subagent's next turn may open on a
    tool-result line, not a prompt."""

    agent_id: str
    path: Path
    offset: int = 0
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)  # child run ids already committed
    open_turn: OpenTurn | None = None


@dataclass
class TailState:
    """One session's follow loop: the file cursor, the turn being assembled, and the
    live stream its events are pushed to."""

    session_id: str
    transcript_path: Path
    send_stream: MemoryObjectSendStream[StreamEvents[str]]
    receive_stream: MemoryObjectReceiveStream[StreamEvents[str]]
    stop_event: asyncio.Event
    offset: int = 0
    # Monotonic time of the last pump that read new bytes; drives the idle reclaim.
    last_active: float = field(default_factory=monotonic)
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)  # prompt_ids already committed
    open_turn: OpenTurn | None = None
    subagents: dict[str, SubagentTail] = field(default_factory=dict)
    # agentId -> the parent-turn tool_use_id that spawned it, harvested from the
    # parent's tool-result lines (`toolUseResult.agentId`); stamps each child run's
    # `parent_tool_call_id`.
    subagent_calls: dict[str, str] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None

    @property
    def subagents_dir(self) -> Path:
        # `<slug>/<session-id>.jsonl` keeps its subagents at `<slug>/<session-id>/subagents`.
        return self.transcript_path.with_suffix("") / "subagents"


class ClaudeTranscriptTailer:
    """Follows each native Claude session's transcript as it is written, streaming the
    model timeline live instead of only rebuilding it after the fact.

    One follow loop per session watches the transcript's directory and, on every change,
    reads the file forward from a byte cursor, frames complete newline-delimited lines,
    and feeds them through a `ClaudeRunAccumulator` — the same translation the live
    tentacle and the whole-file restore use. It drives two decoupled sinks:

    - **durable** — one `ExternalAgentRun` per completed turn, carrying the byte range it
      was built from, replacing the provisional run the hooks sketched for the turn
      (`ClaudeHookIngest.sketch_run`). Idempotent by `prompt_id`.
    - **live** — a bounded, drop-on-full `StreamEvents` stream for a future UI, which
      never blocks the tailer and which durability never depends on.

    A turn commits on the *file* boundary — the next prompt line, or EOF at `finalize` —
    never on a hook, so its bytes are provably flushed once the line after it exists.

    Only `recover` resumes from a turn's `end_offset`; a follow loop re-reads its
    transcript whole and lets the committed-turn guard drop what it has already seen.
    Re-reading is what heals a session the tail was absent for, and it costs ~15ms per MB
    (~160ms for a 3987-line, 56-turn session), paid once per start — cheaper than the
    checkpoint's one real risk, resuming past bytes no run ever covered.
    """

    def __init__(
        self,
        conversation_manager: ConversationManager,
        thread_manager: ThreadManager,
        locks: SessionLocks | None = None,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.thread_manager = thread_manager
        # Per-session locks, shared with the hook ingest when wired together, so a turn's
        # run/ledger commit here can't interleave with the hooks' ledger writes for the
        # same session. Its own registry when the tailer runs standalone.
        self.locks = locks if locks is not None else SessionLocks()
        self.sessions: dict[str, TailState] = {}

    def is_following(self, session_id: str) -> bool:
        return session_id in self.sessions

    def start(
        self, session_id: str, transcript_path: Path, *, offset: int = 0
    ) -> TailState:
        """Begin (or rejoin) following a session. One loop per session: a live one on the
        same path is returned as-is, so a repeated start (e.g. `SessionStart` then the
        first `UserPromptSubmit`) is a no-op. `offset` seeds the cursor for a resume."""
        existing = self.sessions.get(session_id)
        if (
            existing is not None
            and existing.task is not None
            and not existing.task.done()
        ):
            if existing.transcript_path == transcript_path:
                return existing
            # Same session, new transcript path (its slug moved): the bytes are the same,
            # only relocated. Follow the new file, re-reading from the open turn's start
            # so a not-yet-committed turn isn't lost; the fresh loop re-seeds its
            # committed-turn guard from the DB, so nothing double-commits.
            #
            # KNOWN ISSUE: unreachable in production, so a slug move is not in fact
            # handled. `ClaudeHookIngest.start_session` — the only caller that ever
            # relocates — returns early while `is_following`, so `existing` is always
            # None here and this branch never runs; the test covering it calls `start`
            # directly, bypassing that guard. The live loop keeps watching the old
            # directory, `pump` swallows `FileNotFoundError` on every wake, and the
            # session goes untailed until the idle timeout reclaims it ~30 min later.
            # Fixing it means letting the ingest hand a changed path through rather than
            # returning early — and then reading `existing.open_turn` here races the
            # loop's own mutation of it (it is None mid-`close_turn`, while `offset` has
            # already advanced past the prompt line), which would silently drop a turn.
            offset = (
                existing.open_turn.start_offset
                if existing.open_turn is not None
                else existing.offset
            )
            existing.task.cancel()
        state = self.new_state(session_id, transcript_path, offset)
        state.task = asyncio.create_task(self.follow(state))
        self.sessions[session_id] = state
        return state

    def new_state(
        self, session_id: str, transcript_path: Path, offset: int = 0
    ) -> TailState:
        send_stream, receive_stream = anyio.create_memory_object_stream[
            StreamEvents[str]
        ](LIVE_STREAM_BUFFER)
        return TailState(
            session_id=session_id,
            transcript_path=transcript_path,
            send_stream=send_stream,
            receive_stream=receive_stream,
            stop_event=asyncio.Event(),
            offset=offset,
        )

    def stream(
        self, session_id: str
    ) -> MemoryObjectReceiveStream[StreamEvents[str]] | None:
        """The live event stream for a session, for a consumer that wants to watch it."""
        state = self.sessions.get(session_id)
        return state.receive_stream if state is not None else None

    async def finalize(
        self, session_id: str, transcript_path: Path | None = None
    ) -> None:
        """End a session's follow loop: it drains to EOF, commits the trailing turn,
        closes the live stream, and drops itself from the registry. Awaits the loop so the
        last turn is durable on return.

        With no loop to end, the session ran unwatched — Octomate restarted mid-session
        and lost the registry, or came up after the session did — so this is the last
        moment its tail can be rescued: `recover` rebuilds from the transcript instead.
        Without that, `SessionEnd` would silently drop every turn since the loop died.
        """
        state = self.sessions.get(session_id)
        if state is None or state.task is None:
            await self.recover(session_id, transcript_path)
            return
        state.stop_event.set()
        await state.task  # its finally drains, commits, and reclaims the registry slot

    async def shutdown(self) -> None:
        """Cancel every follow loop (tentacle disconnect). No final drain — an
        interrupted session's trailing turn is recovered by re-tailing later."""
        tasks = [s.task for s in self.sessions.values() if s.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.sessions.clear()

    async def recover(
        self, session_id: str, transcript_path: Path | None = None
    ) -> list[str]:
        """Re-tail a session's transcript once, from the last committed offset, recording
        any turns not yet persisted — the manual recovery path for a session Octomate
        watched only partially or never (down during the run, hooks not wired). Runs the
        same assembly as the live follow loop, so it rebuilds the model timeline and the
        human ledger identically. Idempotent by `prompt_id` — re-running, or overlapping a
        live follow, is a safe no-op. Resolves a moved slug when no path is given; returns
        the ids of the turns it recovered."""
        path = transcript_path or locate_transcript(session_id)
        if path is None:
            return []
        with claude_logfire.span(
            "claude.tailer.recover [{session_id}]", session_id=session_id
        ):
            # Own a materia context, mirroring the follow task's boundary.
            with sqlalchemy_materia():
                conversation = await self.ensure_session(session_id)
                committed = assembled(conversation)
                start = max(
                    (
                        run.end_offset or 0
                        for run in conversation.runs
                        if isinstance(run, ExternalAgentRun)
                    ),
                    default=0,
                )
                state = self.new_state(session_id, path, start)
                state.conversation = conversation
                state.recorded = set(committed)
                try:
                    await self.pump(
                        state
                    )  # read [start .. EOF), committing on boundaries
                    await self.close_turn(state)  # commit the trailing turn
                    await self.close_subagent_turns(state)
                finally:
                    state.send_stream.close()
                fresh = await self.ensure_session(session_id)
                recovered = sorted(
                    (
                        run
                        for run in fresh.runs
                        if isinstance(run, ExternalAgentRun)
                        and run.end_offset is not None
                        and run.id not in committed
                    ),
                    key=lambda run: run.start_offset or 0,
                )
                logger.info(
                    "session %s: re-tailed from byte %d, recovering %d turn(s) no live "
                    "tail saw",
                    session_id,
                    start,
                    len(recovered),
                )
                return [run.id for run in recovered]

    async def follow(self, state: TailState) -> None:
        """The per-session loop: catch up on what is already on disk, then pump on every
        directory change until `finalize` stops it, and drain once more to EOF."""
        with claude_logfire.span(
            "claude.tailer.follow [{session_id}]",
            session_id=state.session_id,
            start_offset=state.offset,
        ):
            try:
                # Own a materia context: a long-lived follow task outlives the request
                # that started it, mirroring the restore task boundary.
                with sqlalchemy_materia():
                    await self.prepare(state)
                    await self.pump(state)
                    async for _ in awatch(
                        state.transcript_path.parent,
                        stop_event=state.stop_event,
                        recursive=False,
                        rust_timeout=IDLE_POLL_MS,
                        yield_on_timeout=True,
                    ):
                        await self.pump(state)
                        if monotonic() - state.last_active > IDLE_TIMEOUT:
                            logger.info(
                                "session %s: silent for %ss, stopped tailing",
                                state.session_id,
                                IDLE_TIMEOUT,
                            )
                            break
                    await self.pump(state)  # final drain to EOF
                    await self.close_turn(state)  # commit the trailing turn
                    await self.close_subagent_turns(state)
            except Exception:
                logger.exception(
                    "session %s: stopped tailing on error; its remaining turns are left "
                    "for the next prompt or SessionEnd to recover",
                    state.session_id,
                )
            finally:
                state.send_stream.close()
                # Reclaim the registry slot on any exit (idle, finalize, crash), unless a
                # relocation already replaced this state with a fresh loop.
                if self.sessions.get(state.session_id) is state:
                    del self.sessions[state.session_id]

    async def prepare(self, state: TailState) -> None:
        """Resolve the session's conversation and seed the committed-turn guard from it,
        so a resume never re-commits a run the last run already wrote."""
        state.conversation = await self.ensure_session(state.session_id)
        state.recorded = assembled(state.conversation)

    async def ensure_session(self, session_id: str) -> Conversation:
        """Map a native Claude session id to its Octomate home — the session's thread and
        the conversation hanging off it — creating either if this is its first sighting."""
        thread = await self.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "private", session_id, "")
        )
        return await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )

    async def pump(self, state: TailState) -> None:
        """Advance the session: the transcript itself, then its subagents' files. The
        subagent pass runs even when the transcript has no new bytes — a working
        subagent writes while its parent waits silently, so parent quiet is exactly
        when the children have the most to say."""
        await self.pump_transcript(state)
        if await self.pump_subagents(state):
            state.last_active = monotonic()  # child bytes keep the session alive too

    async def pump_transcript(self, state: TailState) -> None:
        """Read the transcript forward from the cursor, framing on raw `\\n`. Only bytes
        up to the last complete line are consumed; a trailing fragment stays unread and
        is re-read next pump, so a line split across two reads is never half-parsed. A
        malformed / unmodeled line is skipped but still advances the cursor, so a bad
        line never wedges ingest."""
        try:
            size = state.transcript_path.stat().st_size
        except FileNotFoundError:
            return  # the file may not exist yet; the dir-watch wakes us when it appears
        if size < state.offset:  # truncation / rotation guard (append-only in practice)
            state.offset = 0
        with state.transcript_path.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read()
        if not chunk:
            return
        state.last_active = monotonic()  # new bytes — the session is alive
        for raw in chunk.split(b"\n")[:-1]:  # last element is the trailing fragment
            start = state.offset
            state.offset += len(raw) + 1  # + the '\n' the line was framed on
            if not raw.strip():
                continue
            try:
                line = transcript_line_adapter.validate_json(raw)
            except ValidationError:
                logger.debug(
                    "skipping unmodeled/malformed transcript line in session %s",
                    state.session_id,
                )
                continue
            await self.process_line(state, line, start, state.offset)

    async def process_line(
        self, state: TailState, line: TranscriptLine, start: int, end: int
    ) -> None:
        """Route one typed line: a `prompt_source` user line opens a turn (closing the
        previous one); other user (tool-result) and assistant lines fold into the open
        turn. Inline `is_sidechain` lines are skipped — transcripts since 2.1.177 keep
        subagents in their own files (`pump_subagents`), so this guards only against an
        older transcript's inline relics."""
        if isinstance(line, TranscriptUserLine):
            if line.is_sidechain:
                return
            self.harvest_subagent_call(state, line)
            if line.prompt_source is not None:
                await self.close_turn(state)
                state.open_turn = self.begin_turn(line, start, end)
            elif state.open_turn is not None:
                self.fold(state, line, end)
        elif isinstance(line, TranscriptAssistantLine):
            if line.is_sidechain:
                return
            if state.open_turn is not None:
                self.fold(state, line, end)

    @staticmethod
    def harvest_subagent_call(state: TailState, line: TranscriptUserLine) -> None:
        """Remember which parent tool call spawned which subagent: an `Agent` call's
        tool-result line names the child (`toolUseResult.agentId`) while its content
        block names the call. Stamps child runs' `parent_tool_call_id` at commit."""
        result = line.tool_use_result
        if not isinstance(result, dict):
            return
        agent_id = result.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            return
        content = line.message.content
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    state.subagent_calls[agent_id] = tool_use_id
                    return

    def fold(
        self,
        state: TailState,
        line: TranscriptAssistantLine | TranscriptUserLine,
        end: int,
    ) -> None:
        """Fold a line into the open turn: consume it (pushing its live events) and
        extend the turn's byte range and provenance to cover it."""
        turn = state.open_turn
        assert turn is not None
        written = len(turn.accumulator.messages)
        for event in turn.accumulator.consume(line):
            self.emit(state, event)
        stamp(turn.accumulator.messages[written:], line.timestamp)
        turn.end_offset = end
        turn.last_line_uuid = line.uuid

    def begin_turn(self, line: TranscriptUserLine, start: int, end: int) -> OpenTurn:
        text = prompt_text(line.message)
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(text)
        stamp(accumulator.messages, line.timestamp)
        return OpenTurn(
            prompt_id=line.prompt_id,
            prompt_text=text,
            source=line.entrypoint,
            start_offset=start,
            end_offset=end,
            accumulator=accumulator,
            last_line_uuid=line.uuid,
        )

    async def close_turn(self, state: TailState) -> None:
        """Commit the open turn as an `ExternalAgentRun` with its byte range, then bind
        the turn's human-ledger rows to it. Idempotent by `prompt_id`, so a re-driven
        overlap after a resume is a safe no-op. Held under the session lock so the commit
        can't interleave with the hooks' ledger writes for the same turn.

        A commit that cannot be made ends the follow loop instead of being skipped past.
        `recover` resumes from the last committed turn's `end_offset`, which only points
        at the right bytes while the committed turns are the *earliest* ones: skipping a
        turn and tailing on would let a later turn push that mark past the gap, stranding
        the skipped turn where no recovery could reach it. Stopping keeps the mark honest
        — the next prompt, or `SessionEnd`, re-reads the turn from there and commits it.
        """
        turn = state.open_turn
        state.open_turn = None
        if turn is None or turn.prompt_id in state.recorded:
            return
        conversation = state.conversation
        assert conversation is not None
        with claude_logfire.span(
            "claude.tailer.commit_turn {prompt_id} [{session_id}]",
            prompt_id=turn.prompt_id,
            session_id=state.session_id,
            start_offset=turn.start_offset,
            end_offset=turn.end_offset,
            messages=len(turn.accumulator.messages),
        ) as span:
            async with self.locks.hold(
                state.session_id, acquire_timeout=COMMIT_LOCK_TIMEOUT
            ):
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=turn.prompt_id,
                    messages=turn.accumulator.messages,
                    name=CLAUDE_NATIVE_ID,
                    external_session_id=state.session_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                    last_line_uuid=turn.last_line_uuid,
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                state.recorded.add(turn.prompt_id)
                await self.bind_ledger(
                    state.session_id,
                    run,
                    turn.prompt_text,
                    turn.accumulator.result_text,
                )
                logger.info(
                    "session %s: turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    turn.prompt_id,
                    len(turn.accumulator.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def pump_subagents(self, state: TailState) -> bool:
        """Advance every subagent transcript of the session, discovering new ones by
        listing the `subagents/` directory. Returns whether any child yielded bytes."""
        directory = state.subagents_dir
        if not directory.is_dir():
            return False
        consumed = False
        for path in sorted(directory.glob("agent-*.jsonl")):
            tail = self.subagent_tail(state, path.stem.removeprefix("agent-"))
            if await self.pump_subagent(state, tail):
                consumed = True
        return consumed

    @staticmethod
    def subagent_tail(state: TailState, agent_id: str) -> SubagentTail:
        tail = state.subagents.get(agent_id)
        if tail is None:
            tail = SubagentTail(
                agent_id=agent_id,
                path=state.subagents_dir / f"agent-{agent_id}.jsonl",
            )
            state.subagents[agent_id] = tail
        return tail

    async def poke_subagent(self, session_id: str, agent_id: str) -> None:
        """Advance one subagent's file right now — a `SubagentStart` hook's precise
        wake, instead of waiting for the parent's next event or the poll tick. A
        session nothing follows is left alone; `recover` rebuilds it later."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        if state.conversation is None:
            # The hook can outrun the follow task's first scheduling slice; prepare
            # synchronously — the manager caches, so racing the task is harmless.
            await self.prepare(state)
        if await self.pump_subagent(state, self.subagent_tail(state, agent_id)):
            state.last_active = monotonic()

    async def finish_subagent(
        self, session_id: str, agent_id: str, *, final_answer: str | None = None
    ) -> None:
        """Drain and commit one subagent on its `SubagentStop` — the child's
        `finalize`. The hook is synchronous but the transcript writer is not: the
        final answer line races it (measured: usually loses by ~1-2KB). When the
        event names the answer, keep draining until the file yields it — the flush
        barrier the hook itself provides — bounded so a mismatch can never hang the
        pipe; without one, allow the writer a single settle beat."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        if state.conversation is None:
            await self.prepare(state)
        tail = self.subagent_tail(state, agent_id)
        await self.pump_subagent(state, tail)
        # The turn this stop is for. If a resume arrives inside the settle window,
        # the pump itself closes this turn on the promptId change and opens the
        # next — which must NOT be committed by this stop, mid-flight.
        target = tail.open_turn
        deadline = monotonic() + SUBAGENT_SETTLE_TIMEOUT
        quiet = 0
        while not self.subagent_settled(tail, final_answer):
            if quiet >= 2:
                break  # writer went silent: the file is as final as it gets
            if monotonic() >= deadline:
                logger.warning(
                    "session %s: subagent %s kept writing past the settle window "
                    "without yielding its announced answer; committing what it "
                    "reached",
                    session_id,
                    agent_id,
                )
                break
            before = tail.offset
            await asyncio.sleep(SUBAGENT_SETTLE_POLL)
            await self.pump_subagent(state, tail)
            quiet = quiet + 1 if tail.offset == before else 0
        if target is None or tail.open_turn is target:
            await self.close_subagent_turn(state, tail)

    @staticmethod
    def subagent_settled(tail: SubagentTail, final_answer: str | None) -> bool:
        """The fast path: the drained turn already carries the exact answer the
        stop event announced. Divergence (a truncated or differently-joined hook
        payload — no contract promises byte equality) is not an error: the caller's
        byte-quiescence fallback settles those, so strictness here only ever costs
        one extra poll beat, never a wrong commit."""
        if final_answer is None:
            return False  # nothing announced; quiescence decides
        turn = tail.open_turn
        if turn is None:
            return False
        return turn.accumulator.result_text.strip() == final_answer.strip()

    async def pump_subagent(self, state: TailState, tail: SubagentTail) -> bool:
        """Read one subagent's file forward from its cursor — the same framing and
        cursor discipline as `pump_transcript`, against the child's own byte space."""
        if tail.conversation is None:
            parent = state.conversation
            assert parent is not None  # prepare()/recover() resolve it before any pump
            tail.conversation = await self.conversation_manager.ensure(
                parent.thread_id,
                agent_tentacle_id=CLAUDE_NATIVE_ID,
                subagent_id=tail.agent_id,
                parent_conversation_id=parent.id,
            )
            tail.recorded = assembled(tail.conversation)
        try:
            size = tail.path.stat().st_size
        except FileNotFoundError:
            return False
        if size < tail.offset:  # truncation guard, mirroring the parent's
            tail.offset = 0
        with tail.path.open("rb") as handle:
            handle.seek(tail.offset)
            chunk = handle.read()
        if not chunk:
            return False
        for raw in chunk.split(b"\n")[:-1]:
            start = tail.offset
            tail.offset += len(raw) + 1
            if not raw.strip():
                continue
            try:
                line = transcript_line_adapter.validate_json(raw)
            except ValidationError:
                logger.debug(
                    "skipping unmodeled/malformed subagent line in session %s/%s",
                    state.session_id,
                    tail.agent_id,
                )
                continue
            await self.process_subagent_line(state, tail, line, start, tail.offset)
        return True

    async def process_subagent_line(
        self,
        state: TailState,
        tail: SubagentTail,
        line: TranscriptLine,
        start: int,
        end: int,
    ) -> None:
        """Frame a subagent's turns on `promptId` change — the only marker its file
        carries (`promptSource` is absent, and every line is `is_sidechain`, so
        neither parent rule applies). Each turn's `promptId` is the *parent* turn
        that drove it; a resumed subagent opens its next turn on whatever user line
        arrives first, which is measured to be a tool result, not a prompt."""
        if isinstance(line, TranscriptUserLine):
            turn = tail.open_turn
            if turn is None or line.prompt_id != turn.prompt_id:
                await self.close_subagent_turn(state, tail)
                tail.open_turn = self.begin_subagent_turn(line, start, end)
            else:
                self.fold_subagent(tail, line, end)
        elif isinstance(line, TranscriptAssistantLine):
            if tail.open_turn is not None:
                self.fold_subagent(tail, line, end)

    def begin_subagent_turn(
        self, line: TranscriptUserLine, start: int, end: int
    ) -> OpenTurn:
        accumulator = ClaudeRunAccumulator()
        text = prompt_text(line.message)
        if text:
            accumulator.begin(text)
        # The opener may itself carry tool results (a resumed subagent's turn opens on
        # one); consuming is a no-op for a plain prompt line.
        for _ in accumulator.consume(line):
            pass
        stamp(accumulator.messages, line.timestamp)
        return OpenTurn(
            prompt_id=line.prompt_id,
            prompt_text=text,
            source=line.entrypoint,
            start_offset=start,
            end_offset=end,
            accumulator=accumulator,
            last_line_uuid=line.uuid,
        )

    def fold_subagent(
        self,
        tail: SubagentTail,
        line: TranscriptAssistantLine | TranscriptUserLine,
        end: int,
    ) -> None:
        """Fold a line into the child's open turn. Events are consumed, not emitted:
        the live stream is the parent timeline's; fanning child events into it would
        interleave two timelines under one label."""
        turn = tail.open_turn
        assert turn is not None
        written = len(turn.accumulator.messages)
        for _ in turn.accumulator.consume(line):
            pass
        stamp(turn.accumulator.messages[written:], line.timestamp)
        turn.end_offset = end
        turn.last_line_uuid = line.uuid

    async def close_subagent_turn(self, state: TailState, tail: SubagentTail) -> None:
        """Commit the child's open turn as a child run: keyed `<agentId>:<promptId>`
        (which subagent, which parent turn — a bare `promptId` would collide with the
        parent run it belongs to), linked by `parent_run_id` = that `promptId` and the
        harvested spawning call. A subagent has no human prompt or answer, so it never
        touches the ledger."""
        turn = tail.open_turn
        tail.open_turn = None
        if turn is None:
            return
        run_id = f"{tail.agent_id}:{turn.prompt_id}"
        if run_id in tail.recorded or not turn.accumulator.messages:
            return
        conversation = tail.conversation
        assert conversation is not None
        with claude_logfire.span(
            "claude.tailer.commit_subagent_turn {run_id} [{session_id}]",
            run_id=run_id,
            session_id=state.session_id,
            start_offset=turn.start_offset,
            end_offset=turn.end_offset,
            messages=len(turn.accumulator.messages),
        ) as span:
            async with self.locks.hold(
                state.session_id, acquire_timeout=COMMIT_LOCK_TIMEOUT
            ):
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=run_id,
                    messages=turn.accumulator.messages,
                    name=CLAUDE_NATIVE_ID,
                    external_session_id=tail.agent_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                    last_line_uuid=turn.last_line_uuid,
                    parent_run_id=turn.prompt_id,
                    parent_tool_call_id=state.subagent_calls.get(tail.agent_id),
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                tail.recorded.add(run_id)
                logger.info(
                    "session %s: subagent %s turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    tail.agent_id,
                    turn.prompt_id,
                    len(turn.accumulator.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def close_subagent_turns(self, state: TailState) -> None:
        """Commit every child's trailing open turn — the subagent counterpart of the
        final `close_turn`, at `finalize` and `recover`."""
        for tail in state.subagents.values():
            await self.close_subagent_turn(state, tail)

    async def bind_ledger(
        self, session_id: str, run: ExternalAgentRun, prompt_text: str, answer: str
    ) -> None:
        """Cross-reference the human ledger (inbound prompt / outbound answer, keyed by
        `prompt_id = run.id`) to this run. Rows the hooks already wrote live are reused;
        any the live pipe never wrote — a session Octomate watched only after the fact,
        via recovery — are created here from the transcript, so the ledger stands alone."""
        thread = await self.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "private", session_id, "")
        )
        prompt_request = next(
            (
                message
                for message in run.messages
                if isinstance(message, ModelRequest) and message.role == "user"
            ),
            None,
        )
        # A row born here is history, so it is dated by the transcript's clock rather
        # than this replay's: `stamp` gave each message the time of the line that
        # produced it, which is when the turn really happened.
        if prompt_request is not None:
            inbound = self.existing_message(thread, run.id, "inbound")
            if inbound is None:
                inbound = await self.thread_manager.record_inbound(
                    MessageEvent(
                        tentacle_id=CLAUDE_NATIVE_ID,
                        message_id=run.id,
                        chat_id=session_id,
                        chat_type="private",
                        user_id=NATIVE_USER.channel_user_id,
                        sender=NATIVE_USER,
                        segments=[TextSegment(data={"text": prompt_text})],
                    ),
                    happened_at=prompt_request.timestamp,
                )
            await self.thread_manager.bind_messages(
                [inbound.id], prompt_request.id, kind="request_source", run_id=run.id
            )
        if answer:
            outbound = self.existing_message(thread, run.id, "outbound")
            if outbound is None:
                answered = next(
                    (
                        message
                        for message in reversed(run.messages)
                        if isinstance(message, ModelResponse)
                    ),
                    None,
                )
                outbound = await self.thread_manager.record_outbound(
                    thread,
                    agent_tentacle_id=CLAUDE_NATIVE_ID,
                    segments=[MarkdownSegment(data={"text": answer})],
                    sender=NATIVE_USER,
                    platform_message_id=run.id,
                    happened_at=answered.timestamp if answered is not None else None,
                )
            await self.thread_manager.bind_assistant_replies(
                [outbound.id], run_id=run.id
            )

    def existing_message(
        self, thread: Thread, prompt_id: str, direction: ThreadMessageDirection
    ) -> ThreadMessage | None:
        return next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == prompt_id
                and message.direction == direction
            ),
            None,
        )

    def emit(self, state: TailState, event: StreamEvents[str]) -> None:
        try:
            state.send_stream.send_nowait(event)
        except (
            anyio.WouldBlock,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            pass  # drop-on-full / no consumer — never block the tailer
