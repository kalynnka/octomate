"""Assembly of native dsh turns from a streamed session log.

History entries streamed in by `octomate deepseek tail` feed `DeepseekEventTailer`,
which commits each completed turn as an `ExternalAgentRun`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from anyio.abc import ObjectSendStream
from octomate_protocol.stream import SESSION_FILE
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage

from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.nerve import Impulse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import (
    DEEPSEEK_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import deepseek_logfire
from octomate.tentacles.deepseek.adapter import (
    DeepseekRunAccumulator,
    deepseek_metadata,
)
from octomate.tentacles.deepseek.wire import (
    HistoryEntry,
    SessionEvent,
    SessionEventFrame,
    SessionTitle,
    history_entry_adapter,
    permission_preset_of,
    text_of,
    turn_end_of,
    user_message_of,
)
from octomate.tentacles.locks import SessionLocks
from octomate.tentacles.tailer import NativeTurn

logger = logging.getLogger(__name__)

COMMIT_LOCK_TIMEOUT = 30.0

# A `Stop` hook fires at dsh's turn-stopping seam, *before* the turn's
# `turn/end` is emitted and flushed — so the settle waits for the streamed
# close to commit before asking the drain, detached from the hook (which has
# already returned; awaiting there would deadlock the seam against its own
# flush). Bounded: a turn that outlives the wait is left for the next connect.
STOP_SETTLE_TIMEOUT = 5.0


def committed_floor(conversation: Conversation) -> int:
    """The highest event seq any committed turn covers — the stream resumes at
    the seq after it."""
    return max(
        (
            run.end_offset
            for run in conversation.runs
            if isinstance(run, ExternalAgentRun) and run.end_offset is not None
        ),
        default=-1,
    )


def event_time(event: SessionEvent) -> datetime:
    """A session event's own clock — `time` is epoch milliseconds."""
    return datetime.fromtimestamp(event.time / 1000, tz=UTC)


def stamp(messages: list[ModelMessage], timestamp: datetime) -> None:
    """Date the messages an event produced by that event's own clock — the
    accumulator stamps `now`, which is wrong for a turn replayed off the log
    (`AgentRun.started_at` and ordering both read from these)."""
    for message in messages:
        message.timestamp = timestamp


@dataclass
class PendingPrompt:
    """One human `user/message` — the ledger row's raw material."""

    text: str
    time: datetime
    seq: int
    via_gateway: bool


def human_prompt(event: SessionEvent) -> PendingPrompt | None:
    """The human's own words in a `user/message`, or None. dsh logs injected
    user-role messages in the same turn — `agent-instructions`, `plugin`
    runtime context — which are the harness talking, not the ledger's prompt;
    human prompts and late question replies are the person's input."""
    message = user_message_of(event)
    if message is None or message.source.kind not in {"user", "user-question-reply"}:
        return None
    text = text_of(message.content)
    if not text:
        return None
    return PendingPrompt(
        text=text,
        time=event_time(event),
        seq=event.seq,
        via_gateway=message.source.rpc_id is not None,
    )


@dataclass
class OpenTurn:
    """The turn currently being assembled off the stream — its accumulator fed
    entry by entry, its seq range advancing to cover the last one folded in."""

    turn: int | None
    start_seq: int
    end_seq: int
    # The prompt arrived through the /api gateway (it carries the gateway's
    # rpcId) rather than being typed into a local CLI/TUI.
    prompt_via_gateway: bool
    permission_mode: str | None  # Observed permission preset for this turn.
    accumulator: DeepseekRunAccumulator = field(default_factory=DeepseekRunAccumulator)
    native: NativeTurn | None = None


@dataclass
class TailState:
    """One streamed session. Its log lives on the client machine that streams
    it (`octomate deepseek tail`, reading that machine's dsh gateway); the
    stream route feeds `feed_remote`, which drives the per-event assembly.
    `transcript_path` is the client's claim in the client's own namespace, a
    label never opened here."""

    session_id: str
    transcript_path: Path
    cwd: str
    stop_event: asyncio.Event
    # The verified bearer's own profile — who every ledger row this stream
    # writes is attributed to.
    sender: UserProfile
    address: ChannelAddress  # The native surface resolved by the tentacle.
    impulses: ObjectSendStream[Impulse]  # The publisher open for this connection.
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)
    floor: int = -1
    prompts: list[PendingPrompt] = field(default_factory=list)
    open_turn: OpenTurn | None = None
    # Pulses on every committed turn; a relayed `Stop` clears it and waits so
    # the finalize goes out only once the stopped turn is durable (or the
    # bounded wait ran out).
    commit_pulse: asyncio.Event = field(default_factory=asyncio.Event)


class DeepseekEventTailer:
    """Assembles native dsh turns from history entries streamed in by
    `octomate deepseek tail` — the stream is the only assembler: the server
    never opens a session log and never speaks to the client machine's dsh.

    The client reads its local gateway (`session.history`, which serves the
    zstd-framed log decoded and unpacked, cold sessions included) and ships
    each entry as one framed line, the event's dense `seq` standing where a
    file tail's byte offsets stand. Entries fold through the same
    `DeepseekRunAccumulator` the driven tentacle uses; a turn commits on its
    own `turn/end` line as an `ExternalAgentRun` keyed `<session>:<turn>`,
    carrying the seq range it was assembled from — the committed floor a
    reconnecting client is told to resume past. An open turn never commits at
    a connection boundary: the next connect re-streams it whole.
    """

    def __init__(
        self,
        conversation_manager: ConversationManager,
        thread_manager: ThreadManager,
        projects: ProjectManager | None = None,
        locks: SessionLocks | None = None,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.thread_manager = thread_manager
        self.projects = projects if projects is not None else ProjectManager()
        self.locks = locks if locks is not None else SessionLocks()
        self.sessions: dict[str, TailState] = {}

    async def attach_remote(
        self,
        session_id: str,
        transcript_path: Path,
        cwd: str,
        sender: UserProfile,
        address: ChannelAddress,
        impulses: ObjectSendStream[Impulse],
    ) -> tuple[TailState, dict[str, int]]:
        """Register a streamed session and answer where it resumes: the seq
        after the committed floor, recomputed from the durable runs — the
        client holds no cursor of its own. A lingering registration (its
        client died without a close) is replaced; the dead route keeps feeding
        the state it attached, and its commits are idempotent."""
        state = TailState(
            session_id, transcript_path, cwd, asyncio.Event(), sender, address, impulses
        )
        thread = await self.session_thread(session_id, cwd)
        state.conversation = await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=DEEPSEEK_NATIVE_ID
        )
        state.recorded = state.conversation.native_run_ids
        state.floor = committed_floor(state.conversation)
        self.sessions[session_id] = state
        logger.info("session %s: remote tail attached", session_id)
        return state, {SESSION_FILE: state.floor + 1}

    async def feed_remote(self, state: TailState, raw: str) -> None:
        """Fold one history entry; the route validates the sequence framing."""
        try:
            entry = history_entry_adapter.validate_json(raw)
        except ValidationError:
            logger.debug("skipping unparseable dsh history entry")
            return
        await self.process_entry(state, entry)

    def detach_remote(self, state: TailState) -> None:
        """Drop a streamed session's registration. Nothing commits on the way
        out — a dsh turn closes on its own `turn/end` line, so a turn still
        open here is mid-flight, and the next connect re-streams it whole from
        the committed floor."""
        if self.sessions.get(state.session_id) is state:
            del self.sessions[state.session_id]

    async def stop_turn(self, session_id: str) -> None:
        """The turn just stopped — its `Stop` hook fired: wait for the stopped
        turn's streamed close to commit, then ask the drain. Runs detached
        from the hook (dsh's stopping seam waits on the hook serially, ahead
        of the very `turn/end` this waits for). A session no stream covers has
        nothing to settle: its turns land at the next connect."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        state.commit_pulse.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state.commit_pulse.wait(), STOP_SETTLE_TIMEOUT)
        state.stop_event.set()

    async def shutdown(self) -> None:
        self.sessions.clear()

    async def session_thread(self, session_id: str, cwd: str) -> Thread:
        """This session's thread, filed under the project already holding the
        directory it runs in — and under none when no project holds it. As on
        the Claude side, a dsh session is not evidence of a project, so
        nothing is registered here; the cwd is recorded on each run
        regardless."""
        holder = self.projects.resolve(Path(cwd)) if cwd else None
        project = self.projects.get(holder) if holder is not None else None
        return await self.thread_manager.ensure(
            ThreadKey(DEEPSEEK_NATIVE_ID, "thread", session_id),
            project=project,
        )

    async def process_entry(self, state: TailState, entry: HistoryEntry) -> None:
        event = entry.event
        if event.seq <= state.floor:
            return
        if event.type == "session/title":
            try:
                name = SessionTitle.model_validate(event.data).title
            except ValidationError:
                return
            if not name or not name.strip():
                return
            conversation = state.conversation
            assert conversation is not None
            async with self.locks.hold(state.session_id):
                await self.conversation_manager.set_name(conversation, name)
                thread = await self.session_thread(state.session_id, state.cwd)
                await self.thread_manager.rename(thread, name)
            return
        if event.type == "permission/preset":
            preset = permission_preset_of(event)
            await self.record_permission_mode(state, preset)
            if state.open_turn is not None and preset is not None:
                state.open_turn.permission_mode = preset
            return
        if state.open_turn is None:
            if event.type == "user/message":
                prompt = human_prompt(event)
                if prompt is not None:
                    state.prompts.append(prompt)
            elif event.type == "turn/start":
                prompts = state.prompts
                state.prompts = []
                number = (
                    event.data.get("turn") if isinstance(event.data, dict) else None
                )
                number = number if isinstance(number, int) else None
                run_id = (
                    f"{state.session_id}:{number if number is not None else event.seq}"
                )
                state.open_turn = OpenTurn(
                    turn=number,
                    start_seq=prompts[0].seq if prompts else event.seq,
                    end_seq=event.seq,
                    prompt_via_gateway=any(prompt.via_gateway for prompt in prompts),
                    permission_mode=(
                        state.conversation.permission_mode
                        if state.conversation is not None
                        else None
                    ),
                )
                state.open_turn.native = await NativeTurn.open(
                    run_id=run_id,
                    recorded=state.recorded,
                    conversations=self.conversation_manager,
                    threads=self.thread_manager,
                    locks=self.locks,
                    address=state.address,
                    sender=state.sender,
                    impulses=state.impulses,
                    messages=state.open_turn.accumulator.messages,
                )
                if state.open_turn.native is None:
                    return
                for prompt in prompts:
                    await self.record_prompt(state, state.open_turn, prompt)
                await self.fold(state.open_turn, state, entry)
            return
        turn = state.open_turn
        if turn.native is None:
            if event.type == "turn/end":
                state.open_turn = None
            return
        if event.type == "user/message":
            prompt = human_prompt(event)
            if prompt is not None:
                await self.record_prompt(state, turn, prompt)
                turn.prompt_via_gateway = turn.prompt_via_gateway or prompt.via_gateway
        await self.fold(turn, state, entry)
        if turn.accumulator.turn_ended:
            state.open_turn = None
            end = turn_end_of(event)
            if end is not None and end.turn is not None:
                turn.turn = end.turn
            await self.commit_turn(state, turn)

    async def record_prompt(
        self, state: TailState, turn: OpenTurn, prompt: PendingPrompt
    ) -> None:
        """Keep each human input where it occurs, including mid-turn steering.

        The event seq is stable across reconnects, even before dsh reports a turn
        number. Its ledger row grants access while the turn is still streaming.
        """
        assert turn.native is not None
        await turn.native.prompt(
            message_id=f"{state.session_id}:message:{prompt.seq}",
            text=prompt.text,
            timestamp=prompt.time,
            metadata=deepseek_metadata(),
        )

    async def fold(self, turn: OpenTurn, state: TailState, entry: HistoryEntry) -> None:
        """Fold and publish one entry, dating messages by the log's clock.

        Committed seqs never reach the fold. The native thread receives only run
        brackets, with the end delivered after its ledger binding commits.
        Re-streaming an open turn starts it again without waking a reflex.
        """
        accumulator = turn.accumulator
        written = len(accumulator.messages)
        frame = SessionEventFrame(
            type="session/event",
            session_id=state.session_id,
            event=entry.event,
            view=entry.view,
        )
        assert turn.native is not None
        await turn.native.fold(accumulator.consume(frame))
        stamp(accumulator.messages[written:], event_time(entry.event))
        turn.end_seq = entry.event.seq

    async def commit_turn(self, state: TailState, turn: OpenTurn) -> None:
        session_id = state.session_id
        run_key = turn.turn if turn.turn is not None else turn.start_seq
        run_id = f"{session_id}:{run_key}"
        if turn.native is None:
            return
        messages = turn.accumulator.messages
        conversation = state.conversation
        assert conversation is not None
        with deepseek_logfire.span(
            "deepseek.tailer.commit_turn {run_id} [{session_id}]",
            run_id=run_id,
            session_id=session_id,
            start_seq=turn.start_seq,
            end_seq=turn.end_seq,
            messages=len(messages),
        ) as span:
            async with self.locks.hold(session_id, acquire_timeout=COMMIT_LOCK_TIMEOUT):
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=run_id,
                    messages=messages,
                    name=DEEPSEEK_NATIVE_ID,
                    model_name=(
                        turn.accumulator.route.model
                        if turn.accumulator.route is not None
                        else None
                    ),
                    permission_mode=turn.permission_mode,
                    cwd=Path(state.cwd) if state.cwd else None,
                    native_session_id=session_id,
                    source="gateway" if turn.prompt_via_gateway else "local",
                    start_offset=turn.start_seq,
                    end_offset=turn.end_seq,
                )
                span.set_attribute("committed", run is not None)
                await turn.native.close(
                    run, run_id=run_id, answer=turn.accumulator.result_text
                )
                state.floor = max(state.floor, turn.end_seq)
                state.commit_pulse.set()
                logger.info(
                    "session %s: turn %s synced — %d messages, seqs %d-%d",
                    session_id,
                    run_id,
                    len(messages),
                    turn.start_seq,
                    turn.end_seq,
                )

    async def record_permission_mode(
        self, state: TailState, preset: str | None
    ) -> None:
        """Keep the conversation's posture at what the session log last said.
        Observed names are retained even when absent from the driven catalog."""
        conversation = state.conversation
        if conversation is None or preset is None:
            return
        if preset == conversation.permission_mode:
            return
        await self.conversation_manager.set_permission_mode(conversation, preset)
