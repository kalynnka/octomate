from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from octomate_cli.stream import SESSION_FILE
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from uuid_utils import UUID

from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    CODEX_NATIVE_ID,
    Thread,
    ThreadKey,
    ThreadMessageDirection,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import codex_logfire
from octomate.tentacles.agents.codex.adapter import CODEX_PROVIDER_NAME, codex_metadata
from octomate.tentacles.agents.codex.transcript import (
    CODEX_HOME_DIRS,
    RolloutLine,
    SessionMetadata,
    payload_type,
    rollout_line_adapter,
    session_metadata_adapter,
)
from octomate.tentacles.agents.locks import SessionLocks
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)

COMMIT_LOCK_TIMEOUT = 30.0

# Codex flushes the rollout's trailing lines *after* firing the Stop hook (measured:
# `task_complete` lands ~1-3s behind it), so a Stop waits for the stopped turn's
# commit before asking the drain — bounded well under the hook pipe's 10s budget.
STOP_SETTLE_TIMEOUT = 5.0


def assembled(conversation: Conversation) -> set[str]:
    return {
        run.id
        for run in conversation.runs
        if isinstance(run, ExternalAgentRun) and run.end_offset is not None
    }


@dataclass
class OpenTurn:
    turn_id: str
    start_offset: int
    end_offset: int
    timestamp: datetime
    source: str | None
    prompt: str = ""
    messages: list[ModelMessage] = field(default_factory=list)
    answer: str = ""
    pending_calls: dict[str, NativeToolCallPart] = field(default_factory=dict)
    usage: RequestUsage | None = None
    parent_run_id: str | None = None
    parent_tool_call_id: str | None = None


@dataclass(frozen=True)
class SubagentCall:
    parent_run_id: str
    parent_tool_call_id: str
    occurred_at_ms: int


@dataclass
class SubagentTail:
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
    # The turn a relayed `Stop` waits on before asking the drain, and the pulse
    # `close_turn` answers with when that turn commits — set by `stop_turn`, so the
    # wait ends the moment the streamed `task_complete` lands instead of on a poll.
    drain_turn: str | None = None
    drain_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # The client streams from this same machine (a loopback peer): its workspace
    # roots then name server-local directories, so `turn_context` registers them.
    local_client: bool = False


class CodexTranscriptTailer:
    """Assembles Codex turns from rollout lines streamed in by `octomate codex tail`.

    The stream is the only assembler — the server never opens a rollout file, even
    for a session on this machine; a session without a tail keeps only the hooks'
    prompt/final-answer sketch. The rollout replaces that sketch at `task_complete`
    with response items, tool activity, reasoning summaries, and usage. Codex
    documents `transcript_path` for hook convenience while warning that its format
    is unstable, so parsing is deliberately narrow and fail-forward.
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
        # A rollout names every directory of its workspace, not just the one the
        # session opened in; each of those is a project this machine works in.
        self.projects = projects if projects is not None else ProjectManager()
        self.locks = locks if locks is not None else SessionLocks()
        self.sessions: dict[str, TailState] = {}

    async def stop_turn(self, session_id: str, turn_id: str | None) -> None:
        """The turn just stopped — its `Stop` hook fired: wait for the stopped turn
        to become durable, then ask the stream to drain.

        The session's lines feed themselves while this waits: Codex flushes
        `task_complete` a beat after the hook, and `close_turn` pulses `drain_ready`
        the moment the streamed line commits the turn. Only then does `stop_event`
        make the route relay `finalize` — whose drain re-reads the client's files to
        EOF, rescuing exactly the bytes a missed watch wake left behind — and the
        tail exits until the next prompt's launcher. The wait is bounded; a turn
        that outlives it is left for the drain or the next connect to land. A
        session no stream covers has nothing to settle: the hooks' sketch is its
        record until a tail connects."""
        state = self.sessions.get(session_id)
        if state is None:
            return
        state.drain_turn = turn_id
        if turn_id is not None and turn_id not in state.recorded:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.drain_ready.wait(), STOP_SETTLE_TIMEOUT)
        state.stop_event.set()

    async def shutdown(self) -> None:
        self.sessions.clear()

    async def attach_remote(
        self,
        session_id: str,
        transcript_path: Path,
        sender: UserProfile,
        *,
        local_client: bool = False,
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
        idempotent. `local_client` marks a loopback peer, whose workspace roots name
        directories on this machine and so register as projects.
        """
        state = TailState(session_id, transcript_path, asyncio.Event(), sender)
        state.local_client = local_client
        state.conversation = await self.ensure_session(session_id)
        state.recorded = assembled(state.conversation)
        self.sessions[session_id] = state
        logger.info("session %s: remote tail attached", session_id)
        return state, {SESSION_FILE: 0}

    async def feed_remote(
        self, state: TailState, key: str | None, raw: str, start: int, end: int
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
            await self.process_line(state, line, start, end)
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
        self, state: TailState, line: RolloutLine, start: int, end: int
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
        if line.type == "turn_context":
            # Only for a loopback client: a far machine's workspace roots name
            # directories this machine does not have, and a project names local ones.
            # TODO: register remote workspaces once projects can span machines.
            if state.local_client:
                await self.register_workspace(state, line.payload)
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
            return
        turn = state.open_turn
        if turn is None:
            return
        turn.end_offset = end
        if line.type == "event_msg":
            if kind == "user_message":
                prompt = line.payload.get("message")
                if isinstance(prompt, str) and prompt:
                    turn.prompt = prompt
                    turn.messages.append(
                        ModelRequest(
                            parts=[UserPromptPart(content=prompt)],
                            timestamp=line.timestamp,
                            metadata=codex_metadata(),
                        )
                    )
            elif kind == "token_count":
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
                        turn.answer = fallback
                    await self.close_turn(state)
            return
        if line.type == "response_item":
            self.consume_response_item(turn, line)

    async def register_workspace(self, state: TailState, payload: JsonObject) -> None:
        """Register a project for every directory this turn's workspace names.

        A Codex workspace is often several directories at once — measured here, a
        session opened in `inky` carries `[inky, kraken, nautilus, octoview, octotype]`,
        and one opened in `vita/api` carries `[vita/api, vita/web, arcanus]`. Those are
        sibling projects, not extra roots of the one the session sits in: folding them
        into `extra_roots` would make a cwd in `kraken` resolve to `inky` and let a run
        bound to `inky` write into all four.

        So each is ensured on its own, and `ensure` does the rest — a root already
        inside a registered project resolves to it instead of registering again.

        Codex's own tree is skipped. It puts a per-session visualization cache under
        `~/.codex` into the workspace, and a runtime's storage is never a project — the
        same line the ingest draws between a project root and a transcript root.
        """
        roots = payload.get("workspace_roots")
        if not isinstance(roots, list):
            return
        for entry in roots:
            if not isinstance(entry, str) or not entry:
                continue
            root = Path(entry)
            if any(root.is_relative_to(home) for home in CODEX_HOME_DIRS):
                continue
            project = await self.projects.ensure(root, origin="codex")
            logger.debug(
                "session %s: workspace root %s is project %s",
                state.session_id,
                root,
                project.name,
            )

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
        tail.recorded = assembled(tail.conversation)

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
        if turn is None:
            return
        turn.end_offset = end
        if line.type == "event_msg":
            if kind == "token_count":
                turn.usage = self.parse_usage(line.payload)
            elif kind in {"task_complete", "turn_aborted"}:
                ended_id = line.payload.get("turn_id")
                if ended_id == turn.turn_id:
                    fallback = line.payload.get("last_agent_message")
                    if not turn.answer and isinstance(fallback, str):
                        turn.answer = fallback
                    await self.close_subagent_turn(state, tail)
            return
        if line.type == "response_item":
            self.consume_response_item(turn, line)

    async def close_subagent_turn(self, state: TailState, tail: SubagentTail) -> None:
        turn = tail.open_turn
        tail.open_turn = None
        if turn is None or turn.turn_id in tail.recorded or not turn.messages:
            return
        call = next(
            (
                candidate
                for candidate in reversed(state.subagent_calls.get(tail.thread_id, []))
                if candidate.occurred_at_ms <= int(turn.timestamp.timestamp() * 1000)
                and candidate.parent_tool_call_id not in tail.linked_call_ids
            ),
            None,
        )
        if call is not None:
            tail.linked_call_ids.add(call.parent_tool_call_id)
            turn.parent_run_id = call.parent_run_id
            turn.parent_tool_call_id = call.parent_tool_call_id
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
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=turn.turn_id,
                    messages=turn.messages,
                    name=CODEX_NATIVE_ID,
                    cwd=Path(tail.cwd) if tail.cwd else None,
                    external_session_id=tail.thread_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                    parent_run_id=turn.parent_run_id,
                    parent_tool_call_id=turn.parent_tool_call_id,
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                tail.recorded.add(turn.turn_id)
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
        if turn is None or turn.turn_id in state.recorded or not turn.messages:
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
                    cwd=Path(state.cwd) if state.cwd else None,
                    external_session_id=state.session_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                state.recorded.add(turn.turn_id)
                if turn.turn_id == state.drain_turn:
                    state.drain_ready.set()
                await self.bind_ledger(
                    state.session_id, run, turn.prompt, turn.answer, state.sender
                )
                logger.info(
                    "session %s: turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    turn.turn_id,
                    len(turn.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def bind_ledger(
        self,
        session_id: str,
        run: ExternalAgentRun,
        prompt: str,
        answer: str,
        sender: UserProfile,
    ) -> None:
        thread = await self.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", session_id)
        )
        request = next(
            (message for message in run.messages if isinstance(message, ModelRequest)),
            None,
        )
        # A row born here is history, so it is dated by the transcript's clock rather
        # than this replay's: each message carries the time of the line that produced
        # it, which is when the turn really happened.
        if request is not None and prompt:
            inbound = self.existing_message(thread, run.id, "inbound")
            if inbound is None:
                inbound = await self.thread_manager.record_inbound(
                    MessageEvent(
                        tentacle_id=CODEX_NATIVE_ID,
                        message_id=run.id,
                        chat_id=session_id,
                        chat_type="thread",
                        user_id=sender.channel_user_id,
                        sender=sender,
                        segments=[TextSegment(data={"text": prompt})],
                    ),
                    happened_at=request.timestamp,
                )
            elif request.timestamp is not None:
                # A row the hooks wrote live is on the receipt clock, a beat behind
                # the rollout line the run is dated by — left alone, the console
                # sorts the run's work above the prompt that caused it.
                await self.thread_manager.redate_message(inbound, request.timestamp)
            await self.thread_manager.bind_messages(
                [inbound.id], request.id, kind="request_source", run_id=run.id
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
            outbound = self.existing_message(thread, run.id, "outbound")
            if outbound is None:
                outbound = await self.thread_manager.record_outbound(
                    thread,
                    agent_tentacle_id=CODEX_NATIVE_ID,
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
    def consume_response_item(turn: OpenTurn, line: RolloutLine) -> None:
        payload = line.payload
        kind = payload_type(line)
        if kind == "message" and payload.get("role") == "assistant":
            texts: list[str] = []
            content = payload.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            texts.append(part_text)
            text = "\n".join(texts)
            if text:
                response_id = payload.get("id")
                turn.messages.append(
                    ModelResponse(
                        parts=[TextPart(content=text)],
                        timestamp=line.timestamp,
                        provider_name=CODEX_PROVIDER_NAME,
                        provider_response_id=response_id
                        if isinstance(response_id, str)
                        else None,
                        metadata=codex_metadata(),
                    )
                )
                if (
                    payload.get("phase") == "final_answer"
                    or payload.get("phase") is None
                ):
                    turn.answer = text
            return
        if kind == "reasoning":
            summary = payload.get("summary")
            texts = []
            if isinstance(summary, list):
                for part in summary:
                    if isinstance(part, str):
                        texts.append(part)
                    elif isinstance(part, dict):
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            texts.append(part_text)
            if texts:
                turn.messages.append(
                    ModelResponse(
                        parts=[ThinkingPart(content="\n".join(texts))],
                        timestamp=line.timestamp,
                        provider_name=CODEX_PROVIDER_NAME,
                        metadata=codex_metadata(),
                    )
                )
            return
        if kind in {"custom_tool_call", "function_call"}:
            call_id = payload.get("call_id")
            name = payload.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                args = payload.get("input", payload.get("arguments", {}))
                turn.pending_calls[call_id] = NativeToolCallPart(
                    tool_name=name,
                    args=args if isinstance(args, str | dict) else {},
                    tool_call_id=call_id,
                    provider_name=CODEX_PROVIDER_NAME,
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
            turn.messages.append(
                ModelResponse(
                    parts=[
                        call,
                        NativeToolReturnPart(
                            tool_name=call.tool_name,
                            content=output,
                            tool_call_id=call_id,
                            provider_name=CODEX_PROVIDER_NAME,
                        ),
                    ],
                    timestamp=line.timestamp,
                    provider_name=CODEX_PROVIDER_NAME,
                    provider_response_id=call_id,
                    metadata=codex_metadata(),
                )
            )

    @staticmethod
    def existing_message(
        thread: Thread, turn_id: str, direction: ThreadMessageDirection
    ):
        return next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == turn_id
                and message.direction == direction
            ),
            None,
        )
