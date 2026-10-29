"""Assembly of native Codex turns from a streamed rollout.

Lines streamed in by `octomate codex tail` feed `CodexTranscriptTailer`, which
commits each completed turn as an `ExternalAgentRun`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

from anyio.abc import ObjectSendStream
from fastapi import UploadFile
from octomate_protocol.stream import SESSION_FILE
from octomate_protocol.visualizations import HtmlSnapshot
from pydantic import ValidationError
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from starlette.datastructures import Headers
from uuid_utils import UUID

from octomate.capabilities.harness.events import ArtifactAvailableEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.files import FileManager
from octomate.managers.thread import ThreadManager
from octomate.nerve import Impulse
from octomate.schemas.artifacts import HtmlArtifact
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import (
    CODEX_NATIVE_ID,
    ThreadKey,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import codex_logfire
from octomate.tentacles.codex.adapter import CODEX_PROVIDER_NAME, codex_metadata
from octomate.tentacles.codex.html import visualization_text
from octomate.tentacles.codex.transcript import (
    RolloutLine,
    SessionMetadata,
    TurnPermissions,
    UserMessageItem,
    payload_type,
    rollout_line_adapter,
    session_metadata_adapter,
)
from octomate.tentacles.locks import SessionLocks
from octomate.tentacles.tailer import NativeTurn
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)

COMMIT_LOCK_TIMEOUT = 30.0


@dataclass
class OpenTurn:
    """The turn being assembled off the stream.

    Opened by a `task_started` line, closed by the `task_complete` or `turn_aborted`
    naming the same turn id.
    """

    turn_id: str
    start_offset: int
    end_offset: int
    timestamp: datetime
    source: str | None
    model_name: str | None = None
    permission_mode: str | None = None
    messages: list[ModelMessage] = field(default_factory=list)
    answer: str = ""
    pending_calls: dict[str, NativeToolCallPart] = field(default_factory=dict)
    usage: RequestUsage | None = None
    part_index: int = 0
    native: NativeTurn | None = None
    visualizations: dict[int, list[HtmlSnapshot]] = field(
        default_factory=dict
    )  # Snapshots keyed by their model message position.
    unpaired_prompts: list[tuple[str, str]] = field(
        default_factory=list
    )  # Source representation and text awaiting their counterpart in a mixed rollout.

    def consume_prompt(self, line: RolloutLine) -> str | None:
        """Read human text once per source occurrence, pairing mirrored formats.

        Pair across representations rather than deduplicating by text: the user
        can send identical prompts twice in one turn, including while it runs.
        """
        if line.type == "event_msg" and payload_type(line) == "user_message":
            text = line.payload.get("message")
            if not isinstance(text, str):
                return None
        elif line.type == "response_item" and line.payload.get("role") == "user":
            try:
                item = UserMessageItem.model_validate(line.payload)
            except ValidationError:
                return None
            kinds = item.metadata.content_item_kinds
            if item.metadata.turn_id != self.turn_id or len(kinds) != len(item.content):
                return None
            text = "\n".join(
                part.text
                for part, kind in zip(item.content, kinds, strict=True)
                if kind == "user.text" and part.type == "input_text" and part.text
            )
        else:
            return None
        if not text:
            return None
        for index, (source, previous) in enumerate(self.unpaired_prompts):
            if source != line.type and previous == text:
                self.unpaired_prompts.pop(index)
                return None
        self.unpaired_prompts.append((line.type, text))
        return text


@dataclass(frozen=True)
class SubagentCall:
    """One `sub_agent_activity` line of the parent rollout.

    The parent turn and tool call that started or prompted a child thread, and
    when, so the child's runs can be linked back to it.
    """

    parent_run_id: str
    parent_tool_call_id: str
    occurred_at_ms: int


@dataclass
class SubagentTail:
    """One child thread's rollout within a session's tail.

    Its own cursor, conversation, and open turn, plus the parent calls already
    linked to its committed runs.
    """

    thread_id: str
    path: Path
    cwd: str = ""  # from the child rollout's own session metadata
    offset: int = 0
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)
    open_turn: OpenTurn | None = None
    own_turns_started: bool = False
    linked_call_ids: set[str] = field(default_factory=set)


@dataclass
class TailState:
    """One streamed session. Its rollout lives on the client machine that streams it
    (`octomate codex tail`); the stream route feeds `feed_remote`, which drives the
    per-line assembly. `transcript_path` is the client's claim in the client's own
    namespace, a label never opened here, and the subagent tails' paths are likewise
    the client's labels for its sibling files."""

    session_id: str
    transcript_path: Path
    stop_event: asyncio.Event
    # The verified bearer's own profile — who every ledger row this stream
    # writes is attributed to.
    sender: UserProfile
    address: ChannelAddress
    impulses: ObjectSendStream[Impulse]
    offset: int = 0
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)
    thread_id: str | None = None
    cwd: str = ""  # from the rollout's session metadata
    source: str | None = None
    open_turn: OpenTurn | None = None
    subagents: dict[str, SubagentTail] = field(default_factory=dict)
    subagent_calls: dict[str, list[SubagentCall]] = field(default_factory=dict)
    classified_rollouts: set[Path] = field(default_factory=set)
    # Stop requests a drain after this turn's terminal transcript line commits.
    drain_turn: str | None = None


class CodexTranscriptTailer:
    """Assembles Codex turns from rollout lines streamed in by `octomate codex tail`.

    The stream is the only assembler — the server never opens a rollout file, even
    for a session on this machine. Hooks reserve empty run identities; only the
    rollout writes prompts, response items, tool activity, reasoning and usage.
    Each connection publishes run brackets and whole-item events at its native
    address. Committed and driven turns stay silent; replay of an open turn starts
    a fresh fold. The end is published only after the run and ledger are durable. Codex
    documents `transcript_path` for hook convenience while warning that its format
    is unstable, so parsing is deliberately narrow and fail-forward.

    A parser update does not repair completed imports. To repair a session, stop
    its tail and verify the full procedure on a consistent database copy first:
    identify only its external runs missing prompts, load those `ExternalAgentRun`
    schemas through Arcanus, clear their `end_offset`, and commit. Re-stream the
    complete original rollout with the same session id and verified sender.
    `record_external_run` replaces the incomplete model history in transcript
    order; `bind_native_run` reuses each existing reply's ledger row by turn id.
    Compare run ids, reply ledger ids, prompt timestamps and bindings before and
    after, and replay again to check idempotency. Model message ids are replaced,
    so inspect shared histories and artifact bindings before selecting any turn.
    Never clear offsets automatically or change a live database without approval.
    """

    def __init__(
        self,
        conversations: ConversationManager,
        threads: ThreadManager,
        files: FileManager,
        locks: SessionLocks | None = None,
    ) -> None:
        self.conversation_manager = conversations
        self.thread_manager = threads
        self.files: FileManager = files
        self.locks = locks if locks is not None else SessionLocks()
        self.sessions: dict[str, TailState] = {}

    async def stop_turn(self, session_id: str, turn_id: str | None) -> None:
        """Request a drain after this turn's terminal line, without holding Stop.

        Codex may flush the reply and `task_complete` only after its hook returns.
        A timeout must not finalize the stream before those bytes arrive. An open
        turn stays attached until its matching terminal line commits; a turn already
        finished can drain immediately. Without a turn id, the idle limit owns exit.
        """
        state = self.sessions.get(session_id)
        if state is None or turn_id is None:
            return
        state.drain_turn = turn_id
        if turn_id in state.recorded and (
            state.open_turn is None or state.open_turn.turn_id != turn_id
        ):
            state.stop_event.set()

    async def shutdown(self) -> None:
        self.sessions.clear()

    async def attach_remote(
        self,
        session_id: str,
        transcript_path: Path,
        sender: UserProfile,
        address: ChannelAddress,
        impulses: ObjectSendStream[Impulse],
    ) -> tuple[TailState, dict[str, int]]:
        """Register a streamed session — its rollout lives on the client machine
        that streams it (`octomate codex tail`) — and answer where its files resume.

        The answer is always byte 0: a rollout's head is load-bearing (`session_meta`
        carries the thread id every child classification checks against, and the
        parent's `sub_agent_activity` lines are what link child runs to their spawning
        calls), so a reconnect re-streams whole files and the committed-turn guard
        skips what is already durable. Child files are not named here either: the
        client keys each by its own label, and `remote_subagent` classifies it from
        its opening `session_meta`.

        A lingering registration (its client died without a close) is replaced; the
        dead route feeds the state object it attached, and its commits are
        idempotent.
        """
        state = TailState(
            session_id, transcript_path, asyncio.Event(), sender, address, impulses
        )
        state.conversation = await self.ensure_session(session_id)
        state.recorded = state.conversation.native_run_ids
        self.sessions[session_id] = state
        logger.info("session %s: remote tail attached", session_id)
        return state, {SESSION_FILE: 0}

    async def feed_remote(
        self,
        state: TailState,
        key: str | None,
        raw: str,
        start: int,
        end: int,
        *,
        visualizations: list[HtmlSnapshot] | None = None,
    ) -> None:
        """Advance a remote session by one framed line — the pumps' per-line body,
        against offsets the client measured in its own file. Takes the state rather
        than a session id so a superseded connection keeps feeding the state it
        attached, never whoever registered after it. `key` is None for the session's
        own rollout, else the client's label for a sibling file."""
        if key is None:
            state.offset = end
            try:
                line = rollout_line_adapter.validate_json(raw)
            except ValidationError:
                logger.debug("skipping unmodeled Codex rollout line")
                return
            await self.process_line(
                state, line, start, end, visualizations=visualizations
            )
            return
        tail = await self.remote_subagent(state, key, raw)
        if tail is None:
            return
        tail.offset = end
        try:
            line = rollout_line_adapter.validate_json(raw)
        except ValidationError:
            logger.debug(
                "skipping unmodeled Codex subagent line in session %s/%s",
                state.session_id,
                tail.thread_id,
            )
            return
        await self.process_subagent_line(state, tail, line, start, end)

    async def remote_subagent(
        self, state: TailState, key: str, raw: str
    ) -> SubagentTail | None:
        """The child tail a client label maps to, classified on first sight from the
        line in hand — `discover_subagent`'s test without the disk read it cannot do
        here. The first line is the file's `session_meta`, because every remote file
        streams from byte 0; a label that names no child of this session is
        remembered and its lines dropped."""
        path = Path(key)
        for tail in state.subagents.values():
            if tail.path == path:
                return tail
        if path in state.classified_rollouts:
            return None
        if state.thread_id is None:
            return None
        try:
            line = rollout_line_adapter.validate_json(raw)
            if line.type != "session_meta":
                return None
            metadata = session_metadata_adapter.validate_python(line.payload)
        except ValidationError:
            logger.debug("skipping remote rollout with malformed metadata: %s", key)
            return None
        state.classified_rollouts.add(path)
        tail = self.subagent_from_metadata(state, metadata, path)
        if tail is None:
            return None
        await self.prepare_subagent(state, tail)
        return tail

    def detach_remote(self, state: TailState) -> None:
        """Drop a remote session's registration. Nothing commits on the way out, eof
        or not — a Codex turn closes on its own `task_complete`/`turn_aborted` line,
        so a turn still open here is mid-flight, and committing it would plant its id
        in the committed-turn guard where the completed version could never replace
        it. The next connect re-streams from byte 0 and the closed turn lands whole."""
        if self.sessions.get(state.session_id) is state:
            del self.sessions[state.session_id]

    async def ensure_session(self, session_id: str) -> Conversation:
        thread = await self.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", session_id)
        )
        return await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )

    async def process_line(
        self,
        state: TailState,
        line: RolloutLine,
        start: int,
        end: int,
        *,
        visualizations: list[HtmlSnapshot] | None = None,
    ) -> None:
        kind = payload_type(line)
        if line.type == "session_meta":
            try:
                metadata = session_metadata_adapter.validate_python(line.payload)
            except ValidationError:
                logger.debug("skipping malformed Codex session metadata")
                return
            state.thread_id = metadata.id or state.thread_id or state.session_id
            state.cwd = metadata.cwd
            state.source = metadata.originator or (
                metadata.source if isinstance(metadata.source, str) else None
            )
            return
        if line.type == "event_msg" and kind == "task_started":
            turn_id = line.payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                return
            if state.open_turn is not None:
                await self.close_turn(state)
            state.open_turn = OpenTurn(
                turn_id=turn_id,
                start_offset=start,
                end_offset=end,
                timestamp=line.timestamp,
                source=state.source,
            )
            state.open_turn.native = await NativeTurn.open(
                run_id=turn_id,
                recorded=state.recorded,
                conversations=self.conversation_manager,
                threads=self.thread_manager,
                locks=self.locks,
                address=state.address,
                sender=state.sender,
                impulses=state.impulses,
                messages=state.open_turn.messages,
            )
            return
        turn = state.open_turn
        if turn is None:
            return
        turn.end_offset = end
        if line.type == "turn_context":
            self.consume_turn_context(turn, line)
            return
        if (
            turn.native is not None
            and (prompt := turn.consume_prompt(line)) is not None
        ):
            await turn.native.prompt(
                message_id=turn.turn_id
                if not turn.native.prompts
                else f"{turn.turn_id}:{start}",
                text=prompt,
                timestamp=line.timestamp,
                metadata=codex_metadata(),
            )
            return
        if line.type == "response_item" and turn.native is not None:
            if visualizations:
                if state.sender.user_id is None:
                    raise ValueError(
                        "HTML capture requires a registered artifact owner"
                    )
                turn.visualizations[len(turn.messages)] = visualizations
            await turn.native.fold(
                self.consume_response_item(turn, line, captured=bool(visualizations))
            )
            return
        if line.type != "event_msg":
            return
        if kind == "token_count":
            turn.usage = self.parse_usage(line.payload)
        elif kind == "sub_agent_activity":
            child_thread_id = line.payload.get("agent_thread_id")
            tool_call_id = line.payload.get("event_id")
            occurred_at_ms = line.payload.get("occurred_at_ms")
            activity_kind = line.payload.get("kind")
            if (
                isinstance(child_thread_id, str)
                and isinstance(tool_call_id, str)
                and isinstance(occurred_at_ms, int)
                and activity_kind in {"started", "interacted"}
            ):
                state.subagent_calls.setdefault(child_thread_id, []).append(
                    SubagentCall(turn.turn_id, tool_call_id, occurred_at_ms)
                )
        elif kind in {"task_complete", "turn_aborted"}:
            ended_id = line.payload.get("turn_id")
            if ended_id == turn.turn_id:
                # An aborted turn is still real work in the file — commit what it
                # reached, as an interrupted Claude turn commits at the next
                # prompt. (`turn_aborted` carries no last message; the fallback
                # read is a no-op there.)
                fallback = line.payload.get("last_agent_message")
                if not turn.answer and isinstance(fallback, str):
                    turn.answer = (
                        visualization_text(fallback, final=True)[0]
                        if turn.visualizations
                        else fallback
                    )
                await self.close_turn(state)
                if ended_id == state.drain_turn:
                    state.stop_event.set()

    def subagent_from_metadata(
        self, state: TailState, metadata: SessionMetadata, path: Path
    ) -> SubagentTail | None:
        """A new child tail when the metadata names a thread-spawned child of this
        session — the test `remote_subagent` runs against a file's streamed opening
        line."""
        source = metadata.source
        spawn = (
            source.subagent.thread_spawn
            if source is not None
            and not isinstance(source, str)
            and source.subagent is not None
            else None
        )
        if (
            spawn is None
            or metadata.session_id != state.session_id
            or spawn.parent_thread_id != state.thread_id
            or not metadata.id
            or self.uuid7_timestamp(metadata.id) is None
        ):
            return None
        tail = SubagentTail(
            thread_id=metadata.id,
            path=path,
            cwd=metadata.cwd,
        )
        state.subagents[tail.thread_id] = tail
        return tail

    async def prepare_subagent(self, state: TailState, tail: SubagentTail) -> None:
        """The child's durable state: its conversation under the parent's, and the
        committed-turn guard seeded from what is already assembled."""
        parent = state.conversation
        assert parent is not None
        tail.conversation = await self.conversation_manager.ensure(
            parent.thread_id,
            agent_tentacle_id=CODEX_NATIVE_ID,
            subagent_id=tail.thread_id,
            parent_conversation_id=parent.id,
        )
        tail.recorded = tail.conversation.native_run_ids
        tail.linked_call_ids = {
            run.parent_tool_call_id
            for run in tail.conversation.runs
            if run.parent_tool_call_id is not None
        }

    async def process_subagent_line(
        self,
        state: TailState,
        tail: SubagentTail,
        line: RolloutLine,
        start: int,
        end: int,
    ) -> None:
        kind = payload_type(line)
        if line.type == "event_msg" and kind == "task_started":
            turn_id = line.payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                return
            if not tail.own_turns_started:
                if not self.turn_belongs_to_subagent(tail.thread_id, turn_id):
                    return
                tail.own_turns_started = True
            await self.close_subagent_turn(state, tail)
            tail.open_turn = OpenTurn(
                turn_id=turn_id,
                start_offset=start,
                end_offset=end,
                timestamp=line.timestamp,
                source="subagent",
            )
            return
        turn = tail.open_turn
        if turn is None or turn.turn_id in tail.recorded:
            return
        turn.end_offset = end
        if line.type == "turn_context":
            self.consume_turn_context(turn, line)
            return
        if (prompt := turn.consume_prompt(line)) is not None:
            turn.messages.append(
                ModelRequest(
                    parts=[UserPromptPart(content=prompt, timestamp=line.timestamp)],
                    timestamp=line.timestamp,
                    metadata=codex_metadata(),
                )
            )
            return
        if line.type == "response_item":
            for _ in self.consume_response_item(turn, line):
                pass
            return
        if line.type != "event_msg":
            return
        if kind == "token_count":
            turn.usage = self.parse_usage(line.payload)
        elif kind in {"task_complete", "turn_aborted"}:
            ended_id = line.payload.get("turn_id")
            if ended_id == turn.turn_id:
                fallback = line.payload.get("last_agent_message")
                if not turn.answer and isinstance(fallback, str):
                    turn.answer = fallback
                await self.close_subagent_turn(state, tail)

    async def close_subagent_turn(self, state: TailState, tail: SubagentTail) -> None:
        turn = tail.open_turn
        tail.open_turn = None
        if turn is None or turn.turn_id in tail.recorded or not turn.messages:
            return
        for message in reversed(turn.messages):
            if isinstance(message, ModelResponse):
                if turn.usage is not None:
                    message.usage = turn.usage
                break
        conversation = tail.conversation
        assert conversation is not None
        with codex_logfire.span(
            "codex.tailer.commit_subagent_turn {turn_id} [{session_id}]",
            turn_id=turn.turn_id,
            session_id=state.session_id,
            child_thread_id=tail.thread_id,
            start_offset=turn.start_offset,
            end_offset=turn.end_offset,
            messages=len(turn.messages),
        ) as span:
            async with self.locks.hold(
                state.session_id, acquire_timeout=COMMIT_LOCK_TIMEOUT
            ):
                call = next(
                    (
                        candidate
                        for candidate in reversed(
                            state.subagent_calls.get(tail.thread_id, [])
                        )
                        if candidate.occurred_at_ms
                        <= int(turn.timestamp.timestamp() * 1000)
                        and candidate.parent_tool_call_id not in tail.linked_call_ids
                    ),
                    None,
                )
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=turn.turn_id,
                    messages=turn.messages,
                    name=CODEX_NATIVE_ID,
                    model_name=turn.model_name,
                    permission_mode=turn.permission_mode,
                    cwd=Path(tail.cwd) if tail.cwd else None,
                    native_session_id=tail.thread_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                    parent_run_id=call.parent_run_id if call is not None else None,
                    parent_tool_call_id=call.parent_tool_call_id
                    if call is not None
                    else None,
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                tail.recorded.add(turn.turn_id)
                if call is not None:
                    tail.linked_call_ids.add(call.parent_tool_call_id)
                logger.info(
                    "session %s: subagent %s turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    tail.thread_id,
                    turn.turn_id,
                    len(turn.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def close_turn(self, state: TailState) -> None:
        turn = state.open_turn
        state.open_turn = None
        if turn is None or turn.native is None:
            return
        for message in reversed(turn.messages):
            if isinstance(message, ModelResponse):
                if turn.usage is not None:
                    message.usage = turn.usage
                break
        conversation = state.conversation
        assert conversation is not None
        with codex_logfire.span(
            "codex.tailer.commit_turn {turn_id} [{session_id}]",
            turn_id=turn.turn_id,
            session_id=state.session_id,
            start_offset=turn.start_offset,
            end_offset=turn.end_offset,
            messages=len(turn.messages),
        ) as span:
            async with self.locks.hold(
                state.session_id, acquire_timeout=COMMIT_LOCK_TIMEOUT
            ):
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=turn.turn_id,
                    messages=turn.messages,
                    name=CODEX_NATIVE_ID,
                    model_name=turn.model_name,
                    permission_mode=turn.permission_mode,
                    cwd=Path(state.cwd) if state.cwd else None,
                    native_session_id=state.session_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                )
                span.set_attribute("committed", run is not None)
                if run is not None:
                    await self.capture_html(state, turn, run)
                await turn.native.close(run, run_id=turn.turn_id, answer=turn.answer)
                logger.info(
                    "session %s: turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    turn.turn_id,
                    len(turn.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def capture_html(
        self, state: TailState, turn: OpenTurn, run: ExternalAgentRun
    ) -> None:
        """Persist client snapshots and bind them to the completed native response.

        The bearer supplies ownership; client paths are labels only. Artifacts use
        the existing file storage, ledger and event stream before the run closes.
        """
        if not turn.visualizations:
            return
        owner_id = state.sender.user_id
        conversation = state.conversation
        assert owner_id is not None
        assert turn.native is not None
        assert conversation is not None
        for position, snapshots in turn.visualizations.items():
            for snapshot in snapshots:
                reference = snapshot.reference
                with BytesIO(snapshot.html.encode("utf-8")) as source:
                    stored = await self.files.write(
                        UploadFile(
                            source,
                            filename=reference.path.name,
                            headers=Headers(
                                {"content-type": "text/html; charset=utf-8"}
                            ),
                        ),
                        owner_id=owner_id,
                    )
                message = await self.thread_manager.record_artifact(
                    conversation.thread_id,
                    HtmlArtifact(
                        owner_id=owner_id,
                        title=reference.title or reference.path.stem,
                        mode=reference.mode,
                        file_id=stored.id,
                    ),
                    sender=state.sender,
                    agent_tentacle_id=CODEX_NATIVE_ID,
                )
                await self.thread_manager.bind_messages(
                    [message.id],
                    run.messages[position].id,
                    kind="assistant_reply",
                    run_id=run.id,
                )
                await turn.native.fold(
                    [
                        ArtifactAvailableEvent(
                            message_id=message.id, artifact_id=message.artifact_id
                        )
                    ]
                )

    @staticmethod
    def turn_belongs_to_subagent(thread_id: str, turn_id: str) -> bool:
        thread_time = CodexTranscriptTailer.uuid7_timestamp(thread_id)
        turn_time = CodexTranscriptTailer.uuid7_timestamp(turn_id)
        return (
            thread_time is not None
            and turn_time is not None
            and turn_time >= thread_time
        )

    @staticmethod
    def uuid7_timestamp(value: str) -> int | None:
        try:
            parsed = UUID(value)
        except ValueError:
            return None
        if parsed.version != 7:
            return None
        return parsed.timestamp

    @staticmethod
    def parse_usage(payload: JsonObject) -> RequestUsage | None:
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        usage = info.get("last_token_usage")
        if not isinstance(usage, dict):
            return None
        values = {key: value for key, value in usage.items() if isinstance(value, int)}
        return RequestUsage(
            input_tokens=values.get("input_tokens", 0),
            output_tokens=values.get("output_tokens", 0),
            cache_read_tokens=values.get("cached_input_tokens", 0),
            details={
                "reasoning_output_tokens": values.get("reasoning_output_tokens", 0)
            },
        )

    @staticmethod
    def consume_turn_context(turn: OpenTurn, line: RolloutLine) -> None:
        model = line.payload.get("model")
        turn.model_name = model if isinstance(model, str) else None
        try:
            turn.permission_mode = TurnPermissions.model_validate(
                line.payload
            ).permission_mode
        except ValueError:
            # Keep ingesting history; an unknown preset makes this turn unforkable.
            turn.permission_mode = None
            logger.warning("Codex turn %s has unsupported permissions", turn.turn_id)

    @staticmethod
    def consume_response_item(
        turn: OpenTurn, line: RolloutLine, *, captured: bool = False
    ) -> Iterator[AgentStreamEvent]:
        """Yield whole items, hiding captured references without editing the rollout."""
        payload = line.payload
        kind = payload_type(line)
        if kind == "message" and payload.get("role") == "assistant":
            texts: list[str] = []
            content = payload.get("content")
            if not isinstance(content, list):
                return
            for part in content:
                if isinstance(part, dict) and isinstance(
                    part_text := part.get("text"), str
                ):
                    texts.append(part_text)
            text = "\n".join(texts)
            if text:
                text = visualization_text(text, final=True)[0] if captured else text
                response_id = payload.get("id")
                part = TextPart(content=text)
                turn.messages.append(
                    ModelResponse(
                        parts=[part],
                        timestamp=line.timestamp,
                        provider_name=CODEX_PROVIDER_NAME,
                        provider_response_id=response_id
                        if isinstance(response_id, str)
                        else None,
                        metadata=codex_metadata(),
                    )
                )
                yield PartStartEvent(index=turn.part_index, part=part)
                yield PartEndEvent(index=turn.part_index, part=part)
                turn.part_index += 1
                if (
                    payload.get("phase") == "final_answer"
                    or payload.get("phase") is None
                ):
                    turn.answer = text
            return
        if kind == "reasoning":
            summary = payload.get("summary")
            texts = []
            if not isinstance(summary, list):
                return
            for part in summary:
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict) and isinstance(
                    part_text := part.get("text"), str
                ):
                    texts.append(part_text)
            if texts:
                part = ThinkingPart(content="\n".join(texts))
                turn.messages.append(
                    ModelResponse(
                        parts=[part],
                        timestamp=line.timestamp,
                        provider_name=CODEX_PROVIDER_NAME,
                        metadata=codex_metadata(),
                    )
                )
                yield PartStartEvent(index=turn.part_index, part=part)
                yield PartEndEvent(index=turn.part_index, part=part)
                turn.part_index += 1
            return
        if kind in {"custom_tool_call", "function_call"}:
            call_id = payload.get("call_id")
            name = payload.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                args = payload.get("input", payload.get("arguments", {}))
                call = NativeToolCallPart(
                    tool_name=name,
                    args=args if isinstance(args, str | dict) else {},
                    tool_call_id=call_id,
                    provider_name=CODEX_PROVIDER_NAME,
                )
                turn.pending_calls[call_id] = call
                yield FunctionToolCallEvent(
                    part=ToolCallPart(
                        tool_name=call.tool_name,
                        args=call.args,
                        tool_call_id=call_id,
                        provider_name=CODEX_PROVIDER_NAME,
                    )
                )
            return
        if kind in {"custom_tool_call_output", "function_call_output"}:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                return
            call = turn.pending_calls.pop(call_id, None)
            if call is None:
                return
            output = payload.get("output", "")
            yield FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name=call.tool_name,
                    content=output,
                    tool_call_id=call_id,
                )
            )
            result = NativeToolReturnPart(
                tool_name=call.tool_name,
                content=output,
                tool_call_id=call_id,
                provider_name=CODEX_PROVIDER_NAME,
            )
            turn.messages.append(
                ModelResponse(
                    parts=[call, result],
                    timestamp=line.timestamp,
                    provider_name=CODEX_PROVIDER_NAME,
                    provider_response_id=call_id,
                    metadata=codex_metadata(),
                )
            )
