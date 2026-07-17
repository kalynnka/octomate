from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic

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
from watchfiles import awatch

from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadKey, ThreadMessageDirection
from octomate.telemetry import codex_logfire
from octomate.tentacles.agent.codex.adapter import CODEX_PROVIDER_NAME, codex_metadata
from octomate.tentacles.agent.codex.ingest import CODEX_NATIVE_ID, NATIVE_USER
from octomate.tentacles.agent.codex.transcript import (
    RolloutLine,
    payload_type,
    rollout_line_adapter,
)
from octomate.tentacles.agent.locks import SessionLocks
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)

COMMIT_LOCK_TIMEOUT = 30.0
IDLE_TIMEOUT = 30 * 60.0
IDLE_POLL_MS = 60_000


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


@dataclass
class TailState:
    session_id: str
    transcript_path: Path
    stop_event: asyncio.Event
    offset: int = 0
    last_active: float = field(default_factory=monotonic)
    conversation: Conversation | None = None
    recorded: set[str] = field(default_factory=set)
    source: str | None = None
    open_turn: OpenTurn | None = None
    nested_turn_ids: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    pump_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CodexTranscriptTailer:
    """Byte-cursor tailer for Codex rollout JSONL files.

    Hooks own the immediate prompt/final-answer sketch. The rollout replaces that
    sketch at `task_complete` with response items, tool activity, reasoning summaries,
    and usage. Codex documents `transcript_path` for hook convenience while warning
    that its format is unstable, so parsing is deliberately narrow and fail-forward.
    """

    def __init__(
        self,
        conversation_manager: ConversationManager,
        thread_manager: ThreadManager,
        locks: SessionLocks | None = None,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.thread_manager = thread_manager
        self.locks = locks if locks is not None else SessionLocks()
        self.sessions: dict[str, TailState] = {}

    def start(self, session_id: str, transcript_path: Path) -> TailState:
        existing = self.sessions.get(session_id)
        if (
            existing is not None
            and existing.task is not None
            and not existing.task.done()
        ):
            return existing
        state = TailState(session_id, transcript_path, asyncio.Event())
        state.task = asyncio.create_task(self.follow(state))
        self.sessions[session_id] = state
        return state

    async def pump_session(self, session_id: str) -> None:
        state = self.sessions.get(session_id)
        if state is not None:
            # A Stop hook can arrive immediately after start, before the follow task's
            # first scheduling slice prepares its durable state. Prepare synchronously
            # here too; manager.ensure is cached, so racing the follow task is harmless.
            if state.conversation is None:
                state.conversation = await self.ensure_session(session_id)
                state.recorded = assembled(state.conversation)
            await self.pump(state)

    async def shutdown(self) -> None:
        tasks = [
            state.task for state in self.sessions.values() if state.task is not None
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.sessions.clear()

    async def follow(self, state: TailState) -> None:
        with codex_logfire.span(
            "codex.tailer.follow [{session_id}]",
            session_id=state.session_id,
            start_offset=state.offset,
        ):
            try:
                with sqlalchemy_materia():
                    state.conversation = await self.ensure_session(state.session_id)
                    state.recorded = assembled(state.conversation)
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
                            break
            except Exception:
                logger.exception(
                    "session %s: stopped tailing Codex rollout", state.session_id
                )
            finally:
                if self.sessions.get(state.session_id) is state:
                    del self.sessions[state.session_id]

    async def ensure_session(self, session_id: str) -> Conversation:
        thread = await self.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "private", session_id, "")
        )
        return await self.conversation_manager.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )

    async def pump(self, state: TailState) -> None:
        async with state.pump_lock:
            try:
                size = state.transcript_path.stat().st_size
            except FileNotFoundError:
                return
            if size < state.offset:
                state.offset = 0
            with state.transcript_path.open("rb") as handle:
                handle.seek(state.offset)
                chunk = handle.read()
            if not chunk:
                return
            state.last_active = monotonic()
            for raw in chunk.split(b"\n")[:-1]:
                start = state.offset
                state.offset += len(raw) + 1
                if not raw.strip():
                    continue
                try:
                    line = rollout_line_adapter.validate_json(raw)
                except ValidationError:
                    logger.debug("skipping unmodeled Codex rollout line")
                    continue
                await self.process_line(state, line, start, state.offset)

    async def process_line(
        self, state: TailState, line: RolloutLine, start: int, end: int
    ) -> None:
        kind = payload_type(line)
        if line.type == "session_meta":
            source = line.payload.get("originator") or line.payload.get("source")
            state.source = source if isinstance(source, str) else None
            return
        if line.type == "event_msg" and kind == "task_started":
            turn_id = line.payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                return
            if state.open_turn is not None:
                # A second task_started while a turn is open: an overlapping task in
                # this same rollout (measured: never a subagent — those write their
                # own rollout file). Track it so its completion is not mistaken for
                # the open turn's; the open turn's byte range stays contiguous.
                state.open_turn.end_offset = end
                state.nested_turn_ids.append(turn_id)
            else:
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
        if state.nested_turn_ids:
            if line.type == "event_msg" and kind in {"task_complete", "turn_aborted"}:
                ended_id = line.payload.get("turn_id")
                if ended_id == state.nested_turn_ids[-1]:
                    state.nested_turn_ids.pop()
                elif ended_id == turn.turn_id:
                    # The open turn itself ended while the stack still held later
                    # task_starteds — those were turns a stuck open turn absorbed,
                    # not overlapping work. Closing honestly here is what stops one
                    # bad turn from swallowing the rest of the session.
                    state.nested_turn_ids.clear()
                    if kind == "task_complete":
                        fallback = line.payload.get("last_agent_message")
                        if not turn.answer and isinstance(fallback, str):
                            turn.answer = fallback
                    await self.close_turn(state)
            return
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
            async with self.locks.hold(state.session_id, timeout=COMMIT_LOCK_TIMEOUT):
                run = await self.conversation_manager.record_external_run(
                    conversation,
                    run_id=turn.turn_id,
                    messages=turn.messages,
                    name=CODEX_NATIVE_ID,
                    external_session_id=state.session_id,
                    source=turn.source,
                    start_offset=turn.start_offset,
                    end_offset=turn.end_offset,
                )
                span.set_attribute("committed", run is not None)
                if run is None:
                    return
                state.recorded.add(turn.turn_id)
                await self.bind_ledger(state.session_id, run, turn.prompt, turn.answer)
                logger.info(
                    "session %s: turn %s synced — %d messages, bytes %d-%d",
                    state.session_id,
                    turn.turn_id,
                    len(turn.messages),
                    turn.start_offset,
                    turn.end_offset,
                )

    async def bind_ledger(
        self, session_id: str, run: ExternalAgentRun, prompt: str, answer: str
    ) -> None:
        thread = await self.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "private", session_id, "")
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
                        chat_type="private",
                        user_id=NATIVE_USER.user_id,
                        sender=NATIVE_USER,
                        segments=[TextSegment(data={"text": prompt})],
                    ),
                    happened_at=request.timestamp,
                )
            await self.thread_manager.bind_messages(
                [inbound.id], request.id, kind="request_source", run_id=run.id
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
                    agent_tentacle_id=CODEX_NATIVE_ID,
                    segments=[MarkdownSegment(data={"text": answer})],
                    platform_message_id=run.id,
                    happened_at=answered.timestamp if answered is not None else None,
                )
            await self.thread_manager.bind_assistant_replies(
                [outbound.id], run_id=run.id
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
