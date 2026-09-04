from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from octomate_cli.stream import SESSION_FILE
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)

from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    DEEPSEEK_NATIVE_ID,
    Thread,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import deepseek_logfire
from octomate.tentacles.agents.deepseek.adapter import (
    DeepseekRunAccumulator,
    deepseek_metadata,
)
from octomate.tentacles.agents.deepseek.wire import (
    HistoryEntry,
    SessionEvent,
    SessionEventFrame,
    history_entry_adapter,
    permission_preset_of,
    text_of,
    turn_end_of,
    user_message_of,
)
from octomate.tentacles.agents.locks import SessionLocks
from octomate.types.permissions import is_deepseek_mode

logger = logging.getLogger(__name__)

COMMIT_LOCK_TIMEOUT = 30.0

# A `Stop` hook fires at dsh's turn-stopping seam, *before* the turn's
# `turn/end` is emitted and flushed — so the settle waits for the streamed
# close to commit before asking the drain, detached from the hook (which has
# already returned; awaiting there would deadlock the seam against its own
# flush). Bounded: a turn that outlives the wait is left for the next connect.
STOP_SETTLE_TIMEOUT = 5.0


def assembled(conversation: Conversation) -> set[str]:
    """The ids of the turns already built from the event stream. The seq range
    is the mark: every run this tailer commits carries one."""
    return {
        run.id
        for run in conversation.runs
        if isinstance(run, ExternalAgentRun) and run.end_offset is not None
    }


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
    only `source.kind == "user"` is the person."""
    message = user_message_of(event)
    if message is None or message.source.kind != "user":
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
    prompt_text: str
    prompt_time: datetime | None
    # The prompt arrived through the /api gateway (it carries the gateway's
    # rpcId) rather than being typed into a local CLI/TUI.
    prompt_via_gateway: bool
    accumulator: DeepseekRunAccumulator = field(default_factory=DeepseekRunAccumulator)


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
        self, session_id: str, transcript_path: Path, cwd: str, sender: UserProfile
    ) -> tuple[TailState, dict[str, int]]:
        """Register a streamed session and answer where it resumes: the seq
        after the committed floor, recomputed from the durable runs — the
        client holds no cursor of its own. A lingering registration (its
        client died without a close) is replaced; the dead route keeps feeding
        the state it attached, and its commits are idempotent."""
        state = TailState(session_id, transcript_path, cwd, asyncio.Event(), sender)
        thread = await self.session_thread(session_id, cwd)
        state.conversation = await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=DEEPSEEK_NATIVE_ID
        )
        state.recorded = assembled(state.conversation)
        state.floor = committed_floor(state.conversation)
        self.sessions[session_id] = state
        logger.info("session %s: remote tail attached", session_id)
        return state, {SESSION_FILE: state.floor + 1}

    async def feed_remote(
        self, state: TailState, key: str | None, raw: str, start: int, end: int
    ) -> None:
        """Advance a streamed session by one framed entry. `key` is always
        None for dsh — a session streams as a single event sequence, and the
        route refuses labeled lines before they reach here."""
        del key, start, end  # contiguity is the route's check, in seq space
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
        if state.open_turn is None:
            if event.type == "user/message":
                prompt = human_prompt(event)
                if prompt is not None:
                    state.prompts.append(prompt)
            elif event.type == "permission/preset":
                await self.record_permission_mode(state, permission_preset_of(event))
            elif event.type == "turn/start":
                prompts = state.prompts
                state.prompts = []
                state.open_turn = OpenTurn(
                    turn=None,
                    start_seq=prompts[0].seq if prompts else event.seq,
                    end_seq=event.seq,
                    prompt_text="\n\n".join(prompt.text for prompt in prompts),
                    prompt_time=prompts[0].time if prompts else None,
                    prompt_via_gateway=any(prompt.via_gateway for prompt in prompts),
                )
                self.fold(state.open_turn, state.session_id, entry)
            return
        turn = state.open_turn
        if event.type == "user/message":
            # The turn's own prompt arrives *inside* it — dsh opens the turn,
            # then splices the inbox into the step — and a steered prompt lands
            # the same way, so every human message joins the turn's ledger row.
            prompt = human_prompt(event)
            if prompt is not None:
                turn.prompt_text = (
                    f"{turn.prompt_text}\n\n{prompt.text}"
                    if turn.prompt_text
                    else prompt.text
                )
                if turn.prompt_time is None:
                    turn.prompt_time = prompt.time
                turn.prompt_via_gateway = turn.prompt_via_gateway or prompt.via_gateway
        self.fold(turn, state.session_id, entry)
        if turn.accumulator.turn_ended:
            state.open_turn = None
            end = turn_end_of(event)
            if end is not None and end.turn is not None:
                turn.turn = end.turn
            await self.commit_turn(state, turn)

    def fold(self, turn: OpenTurn, session_id: str, entry: HistoryEntry) -> None:
        """Fold one entry into the open turn: consume it (the live events are
        nobody's here — no channel watches a native session) and date the
        messages it produced by the event's own clock."""
        accumulator = turn.accumulator
        written = len(accumulator.messages)
        frame = SessionEventFrame(
            type="session/event",
            session_id=session_id,
            event=entry.event,
            view=entry.view,
        )
        for _ in accumulator.consume(frame):
            pass
        stamp(accumulator.messages[written:], event_time(entry.event))
        turn.end_seq = entry.event.seq

    async def commit_turn(self, state: TailState, turn: OpenTurn) -> None:
        session_id = state.session_id
        run_key = turn.turn if turn.turn is not None else turn.start_seq
        run_id = f"{session_id}:{run_key}"
        if run_id in state.recorded or not turn.accumulator.messages:
            return
        messages: list[ModelMessage] = []
        if turn.prompt_text:
            messages.append(
                ModelRequest(
                    parts=[UserPromptPart(content=turn.prompt_text)],
                    timestamp=turn.prompt_time,
                    metadata=deepseek_metadata(),
                )
            )
        messages.extend(turn.accumulator.messages)
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
                    cwd=Path(state.cwd) if state.cwd else None,
                    external_session_id=session_id,
                    source="gateway" if turn.prompt_via_gateway else "local",
                    start_offset=turn.start_seq,
                    end_offset=turn.end_seq,
                )
                span.set_attribute("committed", run is not None)
                state.recorded.add(run_id)
                state.floor = max(state.floor, turn.end_seq)
                state.commit_pulse.set()
                if run is None:
                    return
                await self.bind_ledger(state, run, turn)
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
        Observed, never set — and a preset this build's vocabulary lacks is
        skipped rather than stored, since the column is validated on read."""
        conversation = state.conversation
        if conversation is None or preset is None:
            return
        if preset == conversation.permission_mode:
            return
        if not is_deepseek_mode(preset):
            logger.debug(
                "session %s reports permission preset %r, which this build does "
                "not model; leaving the conversation at %r",
                state.session_id,
                preset,
                conversation.permission_mode,
            )
            return
        await self.conversation_manager.set_permission_mode(conversation, preset)

    async def bind_ledger(
        self, state: TailState, run: ExternalAgentRun, turn: OpenTurn
    ) -> None:
        """The turn's human ledger — its prompt and answer as thread messages,
        dated by the log's clock and bound to the run. Written here rather than
        live at the hooks: dsh's hook dialect carries no per-turn key and no
        answer, so the stream's assembly is the first moment both rows can
        exist deduplicated."""
        thread = await self.thread_manager.ensure(
            ThreadKey(DEEPSEEK_NATIVE_ID, "thread", state.session_id)
        )
        request = next(
            (message for message in run.messages if isinstance(message, ModelRequest)),
            None,
        )
        if request is not None and turn.prompt_text:
            inbound = await self.thread_manager.find_message(
                thread.id, run.id, "inbound"
            )
            if inbound is None:
                inbound = await self.thread_manager.record_inbound(
                    MessageEvent(
                        tentacle_id=DEEPSEEK_NATIVE_ID,
                        message_id=run.id,
                        chat_id=state.session_id,
                        chat_type="thread",
                        user_id=state.sender.channel_user_id,
                        sender=state.sender,
                        segments=[TextSegment(data={"text": turn.prompt_text})],
                    ),
                    happened_at=request.timestamp,
                )
            await self.thread_manager.bind_messages(
                [inbound.id], request.id, kind="request_source", run_id=run.id
            )
        answer = turn.accumulator.result_text
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
                    agent_tentacle_id=DEEPSEEK_NATIVE_ID,
                    segments=[MarkdownSegment(data={"text": answer})],
                    sender=state.sender,
                    platform_message_id=run.id,
                    happened_at=answered.timestamp if answered is not None else None,
                )
            await self.thread_manager.bind_assistant_replies(
                [outbound.id], run_id=run.id
            )
