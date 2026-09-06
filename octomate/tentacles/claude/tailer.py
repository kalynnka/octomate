from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from octomate_protocol.stream import SESSION_FILE
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage as PydanticModelMessage

from octomate.capabilities.harness.events import StreamEvents
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    CLAUDE_NATIVE_ID,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import claude_logfire
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transcript import (
    TranscriptAiTitleLine,
    TranscriptAssistantLine,
    TranscriptLine,
    TranscriptUserLine,
    prompt_text,
    transcript_line_adapter,
)
from octomate.tentacles.locks import SessionLocks
from octomate.types.permissions import is_claude_mode

logger = logging.getLogger(__name__)

# The live stream is a forward-looking convenience for a future UI channel: bounded and
# drop-on-full, so a session nobody is watching never backpressures the tailer.
# Durability rests entirely on the `ExternalAgentRun` sink, never on a consumer existing.
LIVE_STREAM_BUFFER = 256

# Bound the wait for the session lock when committing a turn, so a wedged hook holder
# can't hang the stream that feeds it. Timing out propagates — see `close_turn` for
# why a turn that can't commit must stop the tail rather than be skipped past.
COMMIT_LOCK_TIMEOUT = 30.0

# A subagent's final answer line races its synchronous SubagentStop hook — measured
# live, 2 of 3 children had it land ~1-2KB after the hook fired. The event names the
# answer (`last_assistant_message`), so `finish_subagent` drains until the file yields
# it, bounded by this window, then closes with what there is.
SUBAGENT_SETTLE_TIMEOUT = 2.0
SUBAGENT_SETTLE_POLL = 0.2

# Bound `finalize`'s wait for a remote session's drain: the stream route relays the
# finalize to the client, which answers with its EOF — but a client that is gone
# cannot, and the SessionEnd hook this wait sits under must not hang on it. Under the
# hook's own 10s budget; an undrained session's turns wait for the next connect.
REMOTE_DRAIN_TIMEOUT = 5.0


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
    commits as an `ExternalAgentRun` when the next prompt line closes it, when its own
    `Stop` hook reaches `stop_turn`, or at `finalize`."""

    prompt_id: str
    prompt_text: str  # the human's clean prompt, for creating the inbound ledger row
    source: str | None  # the transcript `entrypoint` (claude-vscode / cli / …)
    # Where the turn's opening prompt line says it ran — the opening line specifically,
    # because a transcript carries a `cwd` on every line and a turn's tools wander into
    # scratchpads and site-packages, so the last line folded in is not where the run
    # happened. Per turn rather than per session, because a session resumed from another
    # directory carries on in that one.
    cwd: str
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
    """One streamed session: its transcript lives on the client machine that streams
    it (`octomate claude tail`); the stream route feeds `feed_remote`, which drives
    the per-line assembly. `transcript_path` is the client's claim in the client's
    own namespace, a label never opened here — as are the subagent tails' paths."""

    session_id: str
    transcript_path: Path
    send_stream: MemoryObjectSendStream[StreamEvents[str]]
    receive_stream: MemoryObjectReceiveStream[StreamEvents[str]]
    stop_event: asyncio.Event
    # The verified bearer's own profile — who every ledger row this stream
    # writes is attributed to.
    sender: UserProfile
    offset: int = 0
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)  # prompt_ids already committed
    open_turn: OpenTurn | None = None
    subagents: dict[str, SubagentTail] = field(default_factory=dict)
    # agentId -> the parent-turn tool_use_id that spawned it, harvested from the
    # parent's tool-result lines (`toolUseResult.agentId`); stamps each child run's
    # `parent_tool_call_id`.
    subagent_calls: dict[str, str] = field(default_factory=dict)
    # The prompt id of a turn whose `Stop` hook fired (`stop_turn`): commit it without
    # waiting for the next prompt line — but commit nothing newer, so a prompt queued
    # into the drain window is left open for the next connect to re-stream.
    drain_turn: str | None = None
    # Set once the session's trailing turns are committed (`finish_remote`), so
    # `finalize` can bound its wait for the client's drain.
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def subagents_dir(self) -> Path:
        # `<slug>/<session-id>.jsonl` keeps its subagents at `<slug>/<session-id>/subagents`.
        return self.transcript_path.with_suffix("") / "subagents"


class ClaudeTranscriptTailer:
    """Assembles Claude turns from transcript lines streamed in by `octomate claude
    tail` — the only assembler: the server never opens a transcript, even for a
    session on this machine, and a session no tail streams keeps only the hooks'
    prompt/answer sketch.

    Each framed line feeds a `ClaudeRunAccumulator` — the same translation the live
    tentacle and the whole-file restore use — driving two decoupled sinks:

    - **durable** — one `ExternalAgentRun` per completed turn, carrying the byte range it
      was built from, replacing the provisional run the hooks sketched for the turn
      (`ClaudeHookIngest.sketch_run`). Idempotent by `prompt_id`.
    - **live** — a bounded, drop-on-full `StreamEvents` stream for a future UI, which
      never blocks the tailer and which durability never depends on.

    A turn commits on a proven boundary: the next prompt line, or the drained `eof`
    a relayed `Stop`/`SessionEnd` asks the client for — the transcript is flushed
    before Claude fires those hooks. Nothing newer than a stopped turn commits early
    (`drain_turn`), and the tail process exits once the stopped turn is durable; the
    next prompt's launcher spawns a fresh one. The committed runs' offsets are what
    a reconnecting client is told to resume from, so a connection that drops
    mid-stream leaves its open turn for the next connect to re-stream.
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

    def new_state(
        self,
        session_id: str,
        transcript_path: Path,
        sender: UserProfile,
        offset: int = 0,
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
            sender=sender,
            offset=offset,
        )

    def stream(
        self, session_id: str
    ) -> MemoryObjectReceiveStream[StreamEvents[str]] | None:
        """The live event stream for a session, for a consumer that wants to watch it."""
        state = self.sessions.get(session_id)
        return state.receive_stream if state is not None else None

    async def stop_turn(self, session_id: str, prompt_id: str | None) -> None:
        """The turn just stopped — its `Stop` hook fired: drain it now instead of
        waiting for the next prompt line to close it.

        The stream route relays `finalize` on `stop_event`, the client ships its
        final lines and answers `eof`, and `finish_remote` commits the stopped turn —
        the tail process exits, and the next prompt's launcher spawns a fresh one.
        Scoped by `prompt_id` so nothing newer than the stopped turn ever commits
        early; without one there is nothing to scope, and the turn keeps its old
        boundary. A session no stream covers has nothing to drain — the hooks'
        sketch is its record until a tail connects.
        """
        if prompt_id is None:
            return
        state = self.sessions.get(session_id)
        if state is None:
            return
        state.drain_turn = prompt_id
        state.stop_event.set()

    async def finalize(self, session_id: str) -> None:
        """End a session (`SessionEnd`): relay the drain to its stream — the route
        watches `stop_event` and sends the client `finalize`; the client drains,
        answers `eof`, and `finish_remote` commits the trailing turns and sets
        `drained`. The wait is bounded: a client that is gone leaves its turns for
        the next connect, and the SessionEnd hook must not hang on it. A session no
        stream covers has nothing to finalize."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        state.stop_event.set()
        try:
            await asyncio.wait_for(state.drained.wait(), REMOTE_DRAIN_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "session %s: remote tail did not drain within %ss; its trailing "
                "turns are left for the next connect to recover",
                session_id,
                REMOTE_DRAIN_TIMEOUT,
            )

    async def shutdown(self) -> None:
        for state in self.sessions.values():
            state.send_stream.close()
        self.sessions.clear()

    async def attach_remote(
        self, session_id: str, transcript_path: Path, sender: UserProfile
    ) -> tuple[TailState, dict[str, int]]:
        """Register a streamed session and answer where each of its files resumes.

        The offsets are recomputed from the committed runs, per file: the session
        transcript under `SESSION_FILE`, each known subagent
        under its agent id — so the client holds no durable cursor and a reconnect is
        just re-asking. A lingering registration (its client died without a close) is
        replaced; the dead route feeds the state object it attached, and its commits
        are idempotent.
        """
        state = self.new_state(session_id, transcript_path, sender)
        await self.prepare(state)

        conversation = state.conversation
        assert conversation is not None  # prepare() resolved it
        state.offset = max(
            (
                run.end_offset or 0
                for run in conversation.runs
                if isinstance(run, ExternalAgentRun)
            ),
            default=0,
        )
        offsets: dict[str, int] = {SESSION_FILE: state.offset}
        for child in await self.conversation_manager.subagents(conversation.id):
            if not child.subagent_id:
                continue
            tail = self.subagent_tail(state, child.subagent_id)
            tail.conversation = child
            tail.recorded = assembled(child)
            tail.offset = max(
                (
                    run.end_offset or 0
                    for run in child.runs
                    if isinstance(run, ExternalAgentRun)
                ),
                default=0,
            )
            offsets[child.subagent_id] = tail.offset
        self.sessions[session_id] = state
        logger.info(
            "session %s: remote tail attached, resuming at %s", session_id, offsets
        )
        return state, offsets

    async def feed_remote(
        self, state: TailState, agent_id: str | None, raw: str, start: int, end: int
    ) -> None:
        """Advance a remote session by one framed line — the pumps' per-line body,
        against offsets the client measured in its own file. Takes the state rather
        than a session id so a superseded connection keeps feeding the state it
        attached, never whoever registered after it."""
        if agent_id is None:
            state.offset = end
            line = self.parse_line(raw, state.session_id)
            if line is not None:
                await self.process_line(state, line, start, end)
            return
        tail = self.subagent_tail(state, agent_id)
        if tail.conversation is None:
            await self.prepare_subagent(state, tail)
        tail.offset = end
        line = self.parse_line(raw, state.session_id)
        if line is not None:
            await self.process_subagent_line(state, tail, line, start, end)

    async def finish_remote(self, state: TailState) -> None:
        """A remote session's clean end — the client drained to EOF and said `eof`.

        After the session's own end (`finalize` relayed at `SessionEnd`, or the idle
        drain), commit everything still open. After a relayed `Stop` (`drain_turn`
        set), commit only the stopped turn: a prompt queued into the drain window is
        left open to re-stream whole on the next connect, and subagent turns keep
        their own boundary — `SubagentStop` commits them, and force-closing here
        would truncate a background child still running across turns. Either way,
        release any finalize waiter and reclaim the registry slot."""
        try:
            if state.drain_turn is None:
                await self.close_turn(state)
                await self.close_subagent_turns(state)
            else:
                turn = state.open_turn
                if turn is not None and turn.prompt_id == state.drain_turn:
                    await self.close_turn(state)
        finally:
            state.drained.set()
            self.detach_remote(state)

    def detach_remote(self, state: TailState) -> None:
        """Drop a remote session's registration without committing anything: the
        connection died mid-stream, so its open turns' bytes were never provably
        complete. The next connect resumes from the committed offsets and re-streams
        them."""
        state.send_stream.close()
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
            ThreadKey(CLAUDE_NATIVE_ID, "thread", session_id)
        )
        return await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )

    @staticmethod
    def parse_line(raw: str | bytes, session_id: str) -> TranscriptLine | None:
        """One typed line, or None for a blank / malformed / unmodeled one — skipped
        but never allowed to wedge ingest; the caller advances its cursor either way."""
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            return transcript_line_adapter.validate_json(stripped)
        except ValidationError:
            logger.debug(
                "skipping unmodeled/malformed transcript line in session %s",
                session_id,
            )
            return None

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
                await self.record_permission_mode(state, line.permission_mode)
                state.open_turn = self.begin_turn(line, start, end)
            elif state.open_turn is not None:
                self.fold(state, line, end)
        elif isinstance(line, TranscriptAssistantLine):
            if line.is_sidechain:
                return
            if state.open_turn is not None:
                self.fold(state, line, end)
        elif isinstance(line, TranscriptAiTitleLine):
            await self.record_title(state, line.ai_title)

    async def record_title(self, state: TailState, title: str) -> None:
        """Carry the name Claude gave this session onto the rows that show it.

        The transcript restates the title on every turn and revises it as the work
        turns out to be about something else, so the comparison — not the write —
        is what runs per line. The thread takes it too: a native thread is its
        session, and a name about the work beats the line the thread opened with.
        """
        conversation = state.conversation
        if conversation is None or conversation.name == title.strip():
            return
        await self.conversation_manager.set_name(conversation, title)
        thread = await self.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "thread", state.session_id)
        )
        await self.thread_manager.rename(thread, title)

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

    async def record_permission_mode(
        self, state: TailState, permission_mode: str | None
    ) -> None:
        """Keep the conversation's posture at what the session's own transcript says.

        Every prompt line carries the mode that turn ran under, so a ⇧⇥ in the client
        reaches Octomate on the operator's next message. Observed, never set: nothing
        here can change a running client's mode, and the console shows it read-only.

        A mode Claude has that this build's vocabulary does not is skipped rather than
        stored — the column is validated on read, so an unknown one would take the
        conversation out of circulation instead of merely being unrecognized.
        """
        conversation = state.conversation
        if conversation is None or permission_mode is None:
            return
        if permission_mode == conversation.permission_mode:
            return
        if not is_claude_mode(permission_mode):
            logger.debug(
                "session %s reports permission mode %r, which this build does not "
                "model; leaving the conversation at %r",
                state.session_id,
                permission_mode,
                conversation.permission_mode,
            )
            return
        await self.conversation_manager.set_permission_mode(
            conversation, permission_mode
        )

    def begin_turn(self, line: TranscriptUserLine, start: int, end: int) -> OpenTurn:
        text = prompt_text(line.message)
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(text)
        stamp(accumulator.messages, line.timestamp)
        return OpenTurn(
            prompt_id=line.prompt_id,
            prompt_text=text,
            source=line.entrypoint,
            cwd=line.cwd,
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

        A commit that cannot be made propagates instead of being skipped past. A
        reconnect resumes from the last committed turn's `end_offset`, which only
        points at the right bytes while the committed turns are the *earliest* ones:
        skipping a turn and streaming on would let a later turn push that mark past
        the gap, stranding the skipped turn where no re-stream could reach it.
        Failing keeps the mark honest — the next connect re-streams the turn from
        there and commits it.
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
                    cwd=Path(turn.cwd) if turn.cwd else None,
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
                    state.sender,
                )
                logger.info(
                    "session %s: turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    turn.prompt_id,
                    len(turn.accumulator.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

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

    async def finish_subagent(
        self, session_id: str, agent_id: str, *, final_answer: str | None = None
    ) -> None:
        """Drain and commit one subagent on its `SubagentStop` — the child's
        `finalize`, which a Claude child needs: its turns frame on promptId change,
        so nothing in its own file closes the last one. The hook is synchronous but
        the transcript writer is not: the final answer line races it (measured:
        usually loses by ~1-2KB). The child's lines arrive through `feed_remote` on
        their own; when the event names the answer, wait until the stream yields it,
        bounded so a mismatch can never hang the pipe — `tail.offset` says whether
        the writer has gone quiet. A session no stream covers has nothing to
        settle."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        if state.conversation is None:
            await self.prepare(state)
        tail = self.subagent_tail(state, agent_id)
        # The turn this stop is for. If a resume arrives inside the settle window,
        # the stream itself closes this turn on the promptId change and opens the
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

    async def prepare_subagent(self, state: TailState, tail: SubagentTail) -> None:
        """Resolve the child's conversation under the session's thread and seed its
        committed-turn guard — the first time this child's lines are fed."""
        parent = state.conversation
        assert parent is not None  # prepare() resolves it at attach
        tail.conversation = await self.conversation_manager.ensure(
            parent.thread_id,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            subagent_id=tail.agent_id,
            parent_conversation_id=parent.id,
        )
        tail.recorded = assembled(tail.conversation)

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
            cwd=line.cwd,
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
                    cwd=Path(turn.cwd) if turn.cwd else None,
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
        final `close_turn`, at a session-ending drain (`finish_remote`)."""
        for tail in state.subagents.values():
            await self.close_subagent_turn(state, tail)

    async def bind_ledger(
        self,
        session_id: str,
        run: ExternalAgentRun,
        prompt_text: str,
        answer: str,
        sender: UserProfile,
    ) -> None:
        """Cross-reference the human ledger (inbound prompt / outbound answer, keyed by
        `prompt_id = run.id`) to this run. Rows the hooks already wrote live are reused;
        any the live pipe never wrote — a session streamed in only after the fact —
        are created here from the transcript, so the ledger stands alone."""
        thread = await self.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "thread", session_id)
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
            inbound = await self.thread_manager.find_message(
                thread.id, run.id, "inbound"
            )
            if inbound is None:
                inbound = await self.thread_manager.record_inbound(
                    MessageEvent(
                        tentacle_id=CLAUDE_NATIVE_ID,
                        message_id=run.id,
                        chat_id=session_id,
                        chat_type="thread",
                        user_id=sender.channel_user_id,
                        sender=sender,
                        segments=[TextSegment(data={"text": prompt_text})],
                    ),
                    happened_at=prompt_request.timestamp,
                )
            elif prompt_request.timestamp is not None:
                # A row the hooks wrote live is on the receipt clock, a beat behind
                # the transcript line the run is dated by — left alone, the console
                # sorts the run's work above the prompt that caused it.
                await self.thread_manager.redate_message(
                    inbound, prompt_request.timestamp
                )
            await self.thread_manager.bind_messages(
                [inbound.id], prompt_request.id, kind="request_source", run_id=run.id
            )
        if answer:
            answered = next(
                (
                    message
                    for message in reversed(run.messages)
                    if isinstance(message, ModelResponse)
                ),
                None,
            )
            outbound = await self.thread_manager.find_message(
                thread.id, run.id, "outbound"
            )
            if outbound is None:
                outbound = await self.thread_manager.record_outbound(
                    thread,
                    agent_tentacle_id=CLAUDE_NATIVE_ID,
                    segments=[MarkdownSegment(data={"text": answer})],
                    sender=sender,
                    platform_message_id=run.id,
                    happened_at=answered.timestamp if answered is not None else None,
                )
            elif answered is not None and answered.timestamp is not None:
                await self.thread_manager.redate_message(outbound, answered.timestamp)
            await self.thread_manager.bind_assistant_replies(
                [outbound.id], run_id=run.id
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
