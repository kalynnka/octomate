from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError
from watchfiles import awatch

from octomate.capabilities.events import StreamEvents
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID
from octomate.tentacles.agent.claude.restore import (
    prompt_text,
    stamp,
    transcript_line_adapter,
)
from octomate.tentacles.agent.claude.transcript import (
    TranscriptAssistantLine,
    TranscriptLine,
    TranscriptUserLine,
)

logger = logging.getLogger(__name__)

# The live stream is a forward-looking convenience for a future UI channel: bounded and
# drop-on-full, so a session nobody is watching never backpressures the tailer.
# Durability rests entirely on the `ExternalAgentRun` sink, never on a consumer existing.
LIVE_STREAM_BUFFER = 256


@dataclass
class OpenTurn:
    """The turn currently being assembled off the live tail — its accumulator fed line
    by line, its byte range advancing to cover the last transcript line folded in. It
    commits as an `ExternalAgentRun` when the next prompt line closes it, or at
    `finalize`."""

    prompt_id: str
    source: str | None  # the transcript `entrypoint` (claude-vscode / cli / …)
    start_offset: int  # byte offset of this turn's opening prompt line
    end_offset: int  # byte offset past the last line folded in
    accumulator: ClaudeRunAccumulator
    last_line_uuid: str | None


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
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)  # prompt_ids already committed
    open_turn: OpenTurn | None = None
    task: asyncio.Task[None] | None = None


class ClaudeTranscriptTailer:
    """Follows each native Claude session's transcript as it is written, streaming the
    model timeline live instead of only rebuilding it after the fact.

    One follow loop per session watches the transcript's directory and, on every change,
    reads the file forward from a byte cursor, frames complete newline-delimited lines,
    and feeds them through a `ClaudeRunAccumulator` — the same translation the live
    tentacle and the whole-file restore use. It drives two decoupled sinks:

    - **durable** — one `ExternalAgentRun` per completed turn, carrying the byte range it
      was built from. Because every turn records its offsets, ingest is checkpointed: an
      interrupted session resumes from `max(end_offset)`, idempotent by `prompt_id`.
    - **live** — a bounded, drop-on-full `StreamEvents` stream for a future UI, which
      never blocks the tailer and which durability never depends on.

    A turn commits on the *file* boundary — the next prompt line, or EOF at `finalize` —
    never on a hook, so its bytes are provably flushed once the line after it exists.
    """

    def __init__(
        self,
        conversation_manager: ConversationManager,
        thread_manager: ThreadManager,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.thread_manager = thread_manager
        self.sessions: dict[str, TailState] = {}

    def start(
        self, session_id: str, transcript_path: Path, *, offset: int = 0
    ) -> TailState:
        """Begin (or rejoin) following a session. One loop per session: a live one is
        returned as-is, so a repeated start (e.g. `SessionStart` then the first
        `UserPromptSubmit`) is a no-op. `offset` seeds the cursor for a resume."""
        existing = self.sessions.get(session_id)
        if existing is not None and existing.task is not None:
            if not existing.task.done():
                return existing
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

    async def finalize(self, session_id: str) -> None:
        """End a session's follow loop: it drains to EOF, commits the trailing turn, and
        closes the live stream. Awaits the loop so the last turn is durable on return."""
        state = self.sessions.pop(session_id, None)
        if state is None or state.task is None:
            return
        state.stop_event.set()
        await state.task

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

    async def follow(self, state: TailState) -> None:
        """The per-session loop: catch up on what is already on disk, then pump on every
        directory change until `finalize` stops it, and drain once more to EOF."""
        try:
            # Own a materia context: a long-lived follow task outlives the request that
            # started it, mirroring the restore task boundary.
            with sqlalchemy_materia():
                await self.prepare(state)
                await self.pump(state)
                async for _ in awatch(
                    state.transcript_path.parent,
                    stop_event=state.stop_event,
                    recursive=False,
                ):
                    await self.pump(state)
                await self.pump(state)  # final drain to EOF
                await self.close_turn(state)  # commit the trailing turn
        except Exception:
            logger.exception(
                "Claude transcript tailer for session %s crashed", state.session_id
            )
        finally:
            state.send_stream.close()

    async def prepare(self, state: TailState) -> None:
        """Resolve the session's conversation and seed the committed-turn guard from it,
        so a resume never re-commits a run the last run already wrote."""
        state.conversation = await self.ensure_session(state.session_id)
        state.recorded = {run.id for run in state.conversation.runs}

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
        """Route one typed line by the same rule `split_turns` applies whole-file: a
        `prompt_source` user line opens a turn (closing the previous one); other user
        (tool-result) and assistant lines fold into the open turn; sub-agent lines are
        skipped."""
        if isinstance(line, TranscriptUserLine):
            if line.is_sidechain:
                return
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
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(prompt_text(line.message))
        stamp(accumulator.messages, line.timestamp)
        return OpenTurn(
            prompt_id=line.prompt_id,
            source=line.entrypoint,
            start_offset=start,
            end_offset=end,
            accumulator=accumulator,
            last_line_uuid=line.uuid,
        )

    async def close_turn(self, state: TailState) -> None:
        """Commit the open turn as an `ExternalAgentRun` with its byte range. Idempotent
        by `prompt_id`, so a re-driven overlap after a resume is a safe no-op."""
        turn = state.open_turn
        state.open_turn = None
        if turn is None or turn.prompt_id in state.recorded:
            return
        conversation = state.conversation
        assert conversation is not None
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
        if run is not None:
            state.recorded.add(turn.prompt_id)

    def emit(self, state: TailState, event: StreamEvents[str]) -> None:
        try:
            state.send_stream.send_nowait(event)
        except (
            anyio.WouldBlock,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            pass  # drop-on-full / no consumer — never block the tailer
