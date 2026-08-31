"""UoW-A — the transcript tailer: assemble a native session's turns from streamed
lines, recording an `ExternalAgentRun` per turn (with its byte range) and pushing
live events, resumed from the committed offsets so a reconnect costs no dup or loss.
The stream is the only assembler — the server never opens a transcript file."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from time import monotonic

import anyio
import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import StreamEvents
from octomate.database import async_session
from octomate.schemas.messages import ModelResponse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import MessageBinding, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.agents.claude import tailer as tailer_mod
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer, TailState
from octomate.tentacles.agents.locks import SessionLocks
from octomate.types.json import JsonObject

SENDER = UserProfile(channel_user_id="lu", name="lu")

SESSION_ID = "sess-tail"
SESSION_KEY = ThreadKey(CLAUDE_NATIVE_ID, "thread", SESSION_ID)

# The transcript in the *client's* namespace: a label the server records, never opens.
CLIENT_PATH = Path("/laptop/.claude/projects/-repo") / f"{SESSION_ID}.jsonl"


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def prompt_record(prompt_id: str, text: str, second: int) -> JsonObject:
    return {
        "type": "user",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": SESSION_ID,
        "version": "2.1.0",
        "gitBranch": "main",
        "parentUuid": None,
        "entrypoint": "cli",
        "promptSource": "cli",
        "promptId": prompt_id,
        "uuid": f"u-{prompt_id}",
        "timestamp": f"2026-07-09T10:00:{second:02d}.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def assistant_record(uid: str, second: int, content: list[JsonValue]) -> JsonObject:
    return {
        "type": "assistant",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": SESSION_ID,
        "version": "2.1.0",
        "gitBranch": "main",
        "parentUuid": f"u-{uid}",
        "entrypoint": "cli",
        "uuid": uid,
        "timestamp": f"2026-07-09T10:00:{second:02d}.000Z",
        "message": {
            "id": f"msg-{uid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-8",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "content": content,
        },
    }


def tool_call_record(uid: str, second: int) -> JsonObject:
    return assistant_record(
        uid,
        second,
        [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "tool_use", "id": "toolu-1", "name": "Bash", "input": {"c": "ls"}},
        ],
    )


def tool_result_record(prompt_id: str, uid: str, second: int) -> JsonObject:
    return {
        "type": "user",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": SESSION_ID,
        "version": "2.1.0",
        "gitBranch": "main",
        "parentUuid": uid,
        "entrypoint": "cli",
        "promptId": prompt_id,
        "uuid": f"tr-{uid}",
        "timestamp": f"2026-07-09T10:00:{second:02d}.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu-1", "content": "file.py"}
            ],
        },
    }


def line_bytes(record: JsonObject) -> bytes:
    return (json.dumps(record) + "\n").encode()


def total_bytes(records: list[JsonObject]) -> int:
    return sum(len(line_bytes(record)) for record in records)


TURN_ONE = [
    prompt_record("p1", "list the files", 1),
    tool_call_record("a1", 2),
    tool_result_record("p1", "a1", 3),
    assistant_record("a2", 4, [{"type": "text", "text": "Done."}]),
]
TURN_TWO = [
    prompt_record("p2", "now commit", 6),
    assistant_record("a3", 7, [{"type": "text", "text": "Committed."}]),
]


async def feed_records(
    tailer: ClaudeTranscriptTailer,
    state: TailState,
    records: list[JsonObject],
    *,
    agent_id: str | None = None,
    start: int = 0,
) -> int:
    """Feed records as the stream client frames them — complete lines with the byte
    range each occupies in its own file — returning the offset past the last one."""
    offset = start
    for record in records:
        raw = line_bytes(record)
        end = offset + len(raw)
        await tailer.feed_remote(state, agent_id, raw[:-1].decode(), offset, end)
        offset = end
    return offset


async def stream_in(
    tailer: ClaudeTranscriptTailer,
    records: list[JsonObject],
    *,
    sub: list[JsonObject] | None = None,
) -> TailState:
    """One clean stream round: attach, feed the session's lines (and a child's, keyed
    by agent id), then the drain's `eof` — `finish_remote` commits and detaches."""
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(tailer, state, records)
    if sub is not None:
        await feed_records(tailer, state, sub, agent_id=AGENT_ID)
    await tailer.finish_remote(state)
    return state


async def runs_of(octomate: Octomate) -> list[ExternalAgentRun]:
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    runs = [run for run in conversation.runs if isinstance(run, ExternalAgentRun)]
    return sorted(runs, key=lambda run: run.start_offset or 0)


def drain(state: TailState) -> list[StreamEvents[str]]:
    events: list[StreamEvents[str]] = []
    while True:
        try:
            events.append(state.receive_stream.receive_nowait())
        except (anyio.WouldBlock, anyio.EndOfStream):
            return events


async def test_records_runs_with_byte_ranges() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, TURN_ONE + TURN_TWO)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]

    p1, p2 = runs
    assert p1.source == "cli"  # the transcript entrypoint
    assert p1.last_line_uuid == "a2"  # last line folded into turn one
    # Contiguous, gapless byte ranges over the file: p1 ends where p2 begins.
    assert p1.start_offset == 0
    assert p1.end_offset == p2.start_offset
    assert p2.end_offset == total_bytes(TURN_ONE + TURN_TWO)

    kinds = [type(message).__name__ for message in p1.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]


async def test_a_burst_assembled_turn_reads_back_in_transcript_order() -> None:
    """The persisted message order is the run relationship's `ModelMessage.id` — ids
    are uuid7, minted in fold order, so id order must equal transcript order even
    when a backfill assembles a whole turn's messages inside one millisecond. This
    is the ingest stream's ordering guarantee: every reader, including the future
    live UI stream, consumes it as-is rather than re-sorting by clock."""
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    await stream_in(tailer, TURN_ONE + TURN_TWO)  # one catch-up burst

    runs = await runs_of(octomate)
    assert len(runs) == 2
    for run in runs:
        ids = [message.id for message in run.messages]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)  # sorted and distinct: strictly increasing
        stamps = [m.timestamp for m in run.messages if m.timestamp is not None]
        assert stamps == sorted(stamps)  # the clocks tell the same story


def test_uuid7_stays_monotonic_inside_a_burst() -> None:
    """`AgentRun.messages` orders by `ModelMessage.id`, and that only equals
    creation order because `uuid_utils`' uuid7 stays monotonic within one
    millisecond (a shared-counter property of the generator). Pinned so a library
    bump that loses it fails loudly instead of silently shuffling burst-assembled
    turns."""
    ids = [uuid7() for _ in range(50_000)]
    assert all(a < b for a, b in pairwise(ids))


async def test_the_posture_a_session_runs_under_is_read_off_its_transcript() -> None:
    """Every prompt line names the mode that turn ran under, so a ⇧⇥ in the client
    reaches Octomate on the operator's next message. Observed, never set — nothing here
    can change a running session, and the console shows it read-only.

    A mode this build does not model is left alone rather than stored: the column is
    validated on read, so an unknown one would take the conversation out of circulation.
    """
    opening = [prompt_record("p1", "list the files", 1) | {"permissionMode": "plan"}]
    switched = prompt_record("p2", "now commit", 6) | {"permissionMode": "acceptEdits"}
    unmodelled = prompt_record("p3", "and push", 8) | {"permissionMode": "hyperdrive"}
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    async def posture() -> str | None:
        thread = await octomate.thread_manager.ensure(SESSION_KEY)
        conversation = await octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        return conversation.permission_mode

    await stream_in(tailer, opening)
    assert await posture() == "plan"

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(
        tailer, state, [switched, unmodelled], start=offsets[tailer_mod.SESSION_FILE]
    )
    await tailer.finish_remote(state)
    assert await posture() == "acceptEdits"


async def test_streams_live_events_to_a_consumer() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    state = await stream_in(tailer, TURN_ONE)

    events = drain(state)
    # The turn's thinking / tool call / result / answer surface as stream events.
    kinds = {type(event).__name__ for event in events}
    assert "FunctionToolCallEvent" in kinds
    assert "FunctionToolResultEvent" in kinds


async def test_no_consumer_still_records_every_run() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # Nobody reads the live stream; drop-on-full must never stall the durable sink.
    await stream_in(tailer, TURN_ONE + TURN_TWO)

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_malformed_line_does_not_stall_ingest() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    # Splice a garbage (unparseable) line in right after turn one's prompt — the
    # client ships every framed line, parseable or not.
    offset = await feed_records(tailer, state, TURN_ONE[:1])
    garbage = '{"type": "assistant", "broken'
    end = offset + len(garbage.encode()) + 1
    await tailer.feed_remote(state, None, garbage, offset, end)
    await feed_records(tailer, state, TURN_ONE[1:] + TURN_TWO, start=end)
    await tailer.finish_remote(state)

    # The bad line is skipped, the offsets advance past it, and both turns record.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_restreaming_committed_bytes_is_idempotent() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, TURN_ONE + TURN_TWO)
    # A client that lost its place re-streams the whole file from zero: every turn
    # is already recorded, so re-reading its bytes commits nothing new.
    await stream_in(tailer, TURN_ONE + TURN_TWO)

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


def wired(octomate: Octomate) -> tuple[ClaudeHookIngest, ClaudeTranscriptTailer]:
    """The ingest + tailer as the tentacle wires them: sharing one per-session lock
    registry so hook ledger writes and tailer run commits serialize."""
    locks = SessionLocks()
    tailer = ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    return ClaudeHookIngest(octomate, tailer, locks), tailer


def hook_event(
    name: str, prompt_id: str | None = None, transcript: Path | None = None, **body: str
) -> ClaudeHookInput:
    return ClaudeHookInput.model_validate(
        {
            "hook_event_name": name,
            "session_id": SESSION_ID,
            **({"prompt_id": prompt_id} if prompt_id is not None else {}),
            **({"transcript_path": str(transcript)} if transcript is not None else {}),
            **body,
        }
    )


async def test_full_lifecycle_records_runs_and_binds_the_ledger() -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    # The hooks write each turn's human ledger as it happens…
    await ingest.handle(
        hook_event("UserPromptSubmit", "p1", CLIENT_PATH, prompt="list the files"),
        SENDER,
    )
    await ingest.handle(
        hook_event("Stop", "p1", last_assistant_message="Done."), SENDER
    )
    await ingest.handle(
        hook_event("UserPromptSubmit", "p2", CLIENT_PATH, prompt="now commit"), SENDER
    )
    await ingest.handle(
        hook_event("Stop", "p2", last_assistant_message="Committed."), SENDER
    )
    # …and the stream is what lands the full timeline.
    await stream_in(tailer, TURN_ONE + TURN_TWO)
    await ingest.handle(
        hook_event("SessionEnd", transcript=CLIENT_PATH, reason="other"), SENDER
    )

    # Both turns recorded as external runs.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]

    # The hooks wrote the human ledger for both turns.
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    ledger = {(m.direction, m.platform_message_id) for m in thread.messages}
    assert {("inbound", "p1"), ("outbound", "p1")} <= ledger
    assert {("inbound", "p2"), ("outbound", "p2")} <= ledger

    # And the tailer bound those rows to their runs.
    async with async_session() as session:
        bindings = await session.list(MessageBinding, limit=None, order_bys=[])
    assert sorted({binding.kind for binding in bindings}) == [
        "assistant_reply",
        "request_source",
    ]
    assert {binding.run_id for binding in bindings} == {"p1", "p2"}


async def test_the_tailer_supersedes_the_hooks_sketch_of_a_turn() -> None:
    """One run per turn across both tiers: the hooks sketch it live from what they see,
    and closing the turn supersedes that with the transcript's full timeline. `prompt_id`
    is the run id both write under, so the sketch is upgraded, never duplicated."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        hook_event("UserPromptSubmit", "p1", CLIENT_PATH, prompt="list the files"),
        SENDER,
    )
    await ingest.handle(
        hook_event("Stop", "p1", last_assistant_message="Done."), SENDER
    )

    # In flight: the prompt and the answer, and no byte range — the mark of a sketch.
    [sketch] = await runs_of(octomate)
    assert sketch.id == "p1"
    assert [message.message_text for message in sketch.messages] == [
        "list the files",
        "Done.",
    ]
    assert sketch.end_offset is None

    await stream_in(tailer, TURN_ONE)

    # Closed: still one run, now carrying the tool round-trip the hooks never saw.
    [run] = await runs_of(octomate)
    assert run.id == "p1"
    assert run.end_offset is not None
    assert [message.kind for message in run.messages] == [
        "request",  # the prompt
        "response",  # the tool call
        "request",  # its result
        "response",  # "Done."
    ]


async def test_a_commit_that_cannot_be_made_propagates_rather_than_skips() -> None:
    """A turn that cannot commit must fail the stream. Skipping it and feeding on
    would let p2 commit with a higher `end_offset`, pushing the resume mark past
    p1's bytes and stranding p1 where no re-stream could reach it."""
    octomate = Octomate()
    locks = SessionLocks()
    tailer = ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    offset = await feed_records(tailer, state, TURN_ONE)

    async with locks.hold(SESSION_ID):  # a wedged holder the commit can't get past
        original = tailer_mod.COMMIT_LOCK_TIMEOUT
        tailer_mod.COMMIT_LOCK_TIMEOUT = 0.05
        try:
            with pytest.raises(TimeoutError):
                # p2's prompt line closes p1, whose commit cannot take the lock.
                await feed_records(tailer, state, TURN_TWO[:1], start=offset)
        finally:
            tailer_mod.COMMIT_LOCK_TIMEOUT = original
    tailer.detach_remote(state)

    assert await runs_of(octomate) == []  # p2 did not commit over p1's failure
    # The resume mark still points at p1's bytes; a re-stream lands both turns.
    await stream_in(tailer, TURN_ONE + TURN_TWO)
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_shutdown_drops_every_registration() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    await tailer.shutdown()

    assert tailer.sessions == {}


def sidechain_record(uid: str, second: int) -> JsonObject:
    """A sub-agent's assistant line — must never land in the parent timeline."""
    record = assistant_record(
        uid, second, [{"type": "text", "text": "subagent chatter"}]
    )
    record["isSidechain"] = True
    return record


def hook(prompt_id: str, **body: str) -> ClaudeHookInput:
    return ClaudeHookInput.model_validate(
        {
            "hook_event_name": "x",
            "session_id": SESSION_ID,
            "prompt_id": prompt_id,
            **body,
        }
    )


async def test_a_streamed_session_reconstructs_full_fidelity() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, [*TURN_ONE, sidechain_record("side", 5), *TURN_TWO])

    first = (await runs_of(octomate))[0]
    assert first.id == "p1"
    assert first.external_session_id == SESSION_ID
    assert first.source == "cli"
    kinds = [type(message).__name__ for message in first.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]

    response = first.messages[1]
    assert isinstance(response, ModelResponse)
    part_kinds = [type(part).__name__ for part in response.parts]
    assert "ThinkingPart" in part_kinds
    assert "ToolCallPart" in part_kinds
    assert response.model_name == "claude-opus-4-8"
    assert response.usage.output_tokens == 7
    assert first.started_at is not None

    # The sub-agent line is excluded from the rebuilt timeline.
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    texts = json.dumps([message.message_text for message in conversation.messages])
    assert "subagent chatter" not in texts


async def test_a_streamed_session_creates_the_human_ledger() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # No hooks ever ran, so the commit creates the ledger from the transcript.
    await stream_in(tailer, TURN_ONE + TURN_TWO)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    ledger = [(m.direction, m.platform_message_id) for m in thread.messages]
    assert ledger == [
        ("inbound", "p1"),
        ("outbound", "p1"),
        ("inbound", "p2"),
        ("outbound", "p2"),
    ]
    async with async_session() as session:
        bindings = await session.list(MessageBinding, limit=None, order_bys=[])
    assert sorted({binding.kind for binding in bindings}) == [
        "assistant_reply",
        "request_source",
    ]


async def test_the_commit_reuses_live_ledger_rows() -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # The hooks already wrote p1's ledger; the commit binds those rows, not duplicates.
    ingest = ClaudeHookIngest(octomate, tailer)
    await ingest.record_prompt(
        hook("p1", prompt="list the files"), "list the files", SENDER
    )
    await ingest.record_answer(hook("p1"), "Done.", SENDER)

    await stream_in(tailer, TURN_ONE + TURN_TWO)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    inbound_p1 = [
        m
        for m in thread.messages
        if m.platform_message_id == "p1" and m.direction == "inbound"
    ]
    assert len(inbound_p1) == 1  # bound the existing row, did not duplicate it


async def test_session_end_with_no_stream_is_a_noop() -> None:
    """No stream ever covered this session — the hooks' sketches are its record, and
    `SessionEnd` has nothing to finalize. The server does not read the path the hook
    names; a backfill is a by-hand `octomate claude tail` run."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        hook_event("SessionEnd", transcript=CLIENT_PATH, reason="other"), SENDER
    )

    assert await runs_of(octomate) == []
    assert tailer.sessions == {}


async def test_a_backfilled_row_is_dated_by_the_transcript_not_the_replay() -> None:
    """A session Octomate only saw after the fact: no hook wrote its ledger, so the
    commit creates those rows itself from the transcript.

    Such a row is history, and the transcript records when the turn really happened —
    `2026-07-09`, days before this replay. Dating it `now` would be a lie the ledger
    keeps, so the transcript's clock is what a backfilled row carries.
    """
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, TURN_ONE)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated  # the commit did write the ledger

    # The prompt line says 10:00:01, the final assistant line 10:00:04.
    assert dated["inbound"] == datetime(2026, 7, 9, 10, 0, 1, tzinfo=UTC)
    assert dated["outbound"] == datetime(2026, 7, 9, 10, 0, 4, tzinfo=UTC)


async def test_commit_redates_the_hooks_ledger_to_the_transcript_clock() -> None:
    """A row the hooks wrote live is stamped at receipt — a beat after Claude wrote
    the line it describes. The commit re-dates the reused rows to the transcript's
    clock, the same one the run is dated by, so the run's cards can never sort above
    the prompt that caused them."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        hook_event("UserPromptSubmit", "p1", CLIENT_PATH, prompt="list the files"),
        SENDER,
    )
    await ingest.handle(
        hook_event("Stop", "p1", last_assistant_message="Done."), SENDER
    )
    await stream_in(tailer, TURN_ONE)

    (p1,) = await runs_of(octomate)
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated["inbound"] == datetime(2026, 7, 9, 10, 0, 1, tzinfo=UTC)
    assert dated["inbound"] == p1.started_at  # one clock: prompt row == run start
    assert dated["outbound"] == datetime(2026, 7, 9, 10, 0, 4, tzinfo=UTC)


AGENT_ID = "abc123def"


def agent_spawn_records(prompt_id: str, second: int) -> list[JsonObject]:
    """The parent-side trace of spawning a subagent: the `Agent` tool call and its
    tool-result line, whose `toolUseResult.agentId` names the child."""
    call = assistant_record(
        f"spawn-{prompt_id}",
        second,
        [
            {
                "type": "tool_use",
                "id": f"toolu-agent-{AGENT_ID}",
                "name": "Agent",
                "input": {"description": "audit", "prompt": "audit the repo"},
            }
        ],
    )
    result = tool_result_record(prompt_id, f"spawn-{prompt_id}", second + 1)
    result["message"] = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": f"toolu-agent-{AGENT_ID}",
                "content": "launched",
            }
        ],
    }
    result["toolUseResult"] = {
        "isAsync": True,
        "status": "async_launched",
        "agentId": AGENT_ID,
    }
    return [call, result]


def sub_user_record(
    prompt_id: str,
    uid: str,
    second: int,
    content: str | list[JsonValue],
) -> JsonObject:
    return {
        "type": "user",
        "isSidechain": True,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": SESSION_ID,
        "version": "2.1.211",
        "gitBranch": "main",
        "parentUuid": None,
        "entrypoint": "cli",
        "promptId": prompt_id,
        "agentId": AGENT_ID,
        "uuid": uid,
        "timestamp": f"2026-07-09T10:01:{second:02d}.000Z",
        "message": {"role": "user", "content": content},
    }


def sub_assistant_record(uid: str, second: int, content: list[JsonValue]) -> JsonObject:
    record = assistant_record(uid, second, content)
    record["isSidechain"] = True
    return record


SUB_TURN_ONE = [
    sub_user_record("p1", "s-u1", 1, [{"type": "text", "text": "audit the repo"}]),
    sub_assistant_record(
        "s-a1",
        2,
        [{"type": "tool_use", "id": "toolu-s1", "name": "Bash", "input": {"c": "ls"}}],
    ),
    sub_user_record(
        "p1",
        "s-u2",
        3,
        [{"type": "tool_result", "tool_use_id": "toolu-s1", "content": "src tests"}],
    ),
    sub_assistant_record("s-a2", 4, [{"type": "text", "text": "two findings"}]),
]
# A resumed subagent's second turn: driven by parent turn p2, and — as measured on
# real transcripts — opening on a tool-result line, not a prompt.
SUB_TURN_TWO = [
    sub_user_record(
        "p2",
        "s-u3",
        6,
        [{"type": "tool_result", "tool_use_id": "toolu-s1", "content": "resumed"}],
    ),
    sub_assistant_record("s-a3", 7, [{"type": "text", "text": "tests are fine"}]),
]


async def subagent_runs_of(octomate: Octomate) -> list[ExternalAgentRun]:
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    parent = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id=CLAUDE_NATIVE_ID,
        subagent_id=AGENT_ID,
        parent_conversation_id=parent.id,
    )
    runs = [run for run in child.runs if isinstance(run, ExternalAgentRun)]
    return sorted(runs, key=lambda run: run.start_offset or 0)


async def test_a_subagent_transcript_becomes_a_child_run() -> None:
    """The subagent's own file is ingested as a child run in its own conversation:
    linked to the parent turn (parent_run_id) and the spawning call
    (parent_tool_call_id), keyed so it can never collide with the parent run, and
    absent from the human ledger — a subagent has no prompt or answer of its own."""
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, turn_one + TURN_TWO, sub=SUB_TURN_ONE)

    # The parent's own turns are untouched by the child's presence.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.parent_run_id == "p1"
    assert child_run.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"
    assert child_run.external_session_id == AGENT_ID
    assert child_run.start_offset == 0
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE)
    kinds = [type(message).__name__ for message in child_run.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]

    # The child conversation names its parent; the ledger never mentions the child.
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    parent = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id=CLAUDE_NATIVE_ID,
        subagent_id=AGENT_ID,
        parent_conversation_id=parent.id,
    )
    assert child.parent_conversation_id == parent.id
    assert all(
        message.platform_message_id != child_run.id for message in thread.messages
    )
    # And the child's timeline never leaks into the parent conversation's history.
    assert all("audit the repo" != message.message_text for message in parent.messages)


async def test_a_resumed_subagent_adds_a_second_run_to_one_conversation() -> None:
    """4 of 93 measured subagent transcripts carry more than one promptId: the
    subagent was resumed by a later parent turn and kept its history. One child
    conversation, one run per parent turn — and the second turn opens on a
    tool-result line, which must still open it."""
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, turn_one + TURN_TWO, sub=SUB_TURN_ONE + SUB_TURN_TWO)

    first, second = await subagent_runs_of(octomate)
    assert first.id == f"{AGENT_ID}:p1"
    assert second.id == f"{AGENT_ID}:p2"
    assert (first.parent_run_id, second.parent_run_id) == ("p1", "p2")
    # Contiguous child byte ranges: the resumed turn starts where the first ended.
    assert first.end_offset == second.start_offset
    assert second.messages, "a tool-result opener still yields a timeline"


async def test_restreaming_a_session_reproduces_the_same_subagent_tree() -> None:
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_in(tailer, turn_one, sub=SUB_TURN_ONE)
    before = [(run.id, run.end_offset) for run in await subagent_runs_of(octomate)]

    # Re-stream the same bytes: idempotent, no duplicate child runs.
    await stream_in(tailer, turn_one, sub=SUB_TURN_ONE)
    after = [(run.id, run.end_offset) for run in await subagent_runs_of(octomate)]
    assert before == after
    assert len(after) == 1


def subagent_hook(name: str, **body: JsonValue) -> ClaudeHookInput:
    return ClaudeHookInput.model_validate(
        {
            "hook_event_name": name,
            "session_id": SESSION_ID,
            "cwd": "/repo",
            "agent_id": AGENT_ID,
            "agent_type": "Explore",
            **body,
        }
    )


async def test_subagent_stop_commits_the_child_run_without_waiting() -> None:
    """The hooks are the child's live close signal: after SubagentStop the child run
    is durable — with its byte range and both parent links — while the stream stays
    attached and the parent's own turn is still open."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    # Parent turn p1 is still in flight: prompt + spawn, no closing prompt after.
    await feed_records(tailer, state, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    await feed_records(tailer, state, SUB_TURN_ONE, agent_id=AGENT_ID)

    await ingest.handle(
        subagent_hook(
            "SubagentStart",
            transcript_path=str(CLIENT_PATH),
            prompt="audit the repo",
        ),
        SENDER,
    )
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings"), SENDER
    )

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE)
    assert child_run.parent_run_id == "p1"
    assert child_run.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"
    # The parent session is still attached and its own turn has not been committed.
    assert tailer.sessions[SESSION_ID] is state
    assert [run.id for run in await runs_of(octomate)] == []
    tailer.detach_remote(state)


async def test_subagent_start_sketches_the_child_run_when_keyed() -> None:
    """With a prompt_id on the event, the child run exists from SubagentStart — a
    dated, provisional sketch the stream later replaces under the same id."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    before = datetime.now(UTC)
    await ingest.handle(
        subagent_hook(
            "SubagentStart",
            transcript_path=str(CLIENT_PATH),
            prompt_id="p1",
            prompt="audit the repo",
        ),
        SENDER,
    )
    after = datetime.now(UTC)

    [sketch] = await subagent_runs_of(octomate)
    assert sketch.id == f"{AGENT_ID}:p1"
    assert sketch.end_offset is None  # provisional: no byte range yet
    assert sketch.parent_run_id == "p1"
    assert sketch.started_at is not None
    assert before <= sketch.started_at <= after
    assert [message.message_text for message in sketch.messages] == ["audit the repo"]

    # The child's lines stream in and the subagent stops: the full timeline replaces
    # the sketch as the same run, now final.
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(tailer, state, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    await feed_records(tailer, state, SUB_TURN_ONE, agent_id=AGENT_ID)
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings"), SENDER
    )
    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE)
    assert len(child_run.messages) == 4
    tailer.detach_remote(state)


async def test_an_event_carrying_agent_id_never_touches_the_parent_turn() -> None:
    """A subagent's own Stop (or any event fired inside one) carries the parent's
    session id; unguarded it would write the parent ledger and sketch the parent
    run with the child's answer."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": SESSION_ID,
                "prompt_id": "p9",
                "prompt": "child noise",
                "agent_id": AGENT_ID,
            }
        ),
        SENDER,
    )
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "Stop",
                "session_id": SESSION_ID,
                "prompt_id": "p9",
                "last_assistant_message": "child answer",
                "agent_id": AGENT_ID,
            }
        ),
        SENDER,
    )

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    assert thread.messages == []  # no inbound, no outbound: the ledger is the human's
    assert await runs_of(octomate) == []
    assert tailer.sessions == {}


async def test_a_driven_sessions_subagent_hooks_are_suppressed() -> None:
    """A subagent event carries the parent's session id, so the driving claim covers
    it for free — pinned here rather than trusted."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    with ingest.driving(SESSION_ID):
        await ingest.handle(
            subagent_hook("SubagentStart", prompt_id="p1", prompt="child work"), SENDER
        )
    assert await subagent_runs_of(octomate) == []
    assert tailer.sessions == {}


async def test_subagent_stop_with_no_stream_is_a_noop() -> None:
    """Nothing is streaming this session, so the stop has nothing to settle: the
    child stays absent until a tail streams its lines in. The server does not read
    the path the hook names."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        subagent_hook(
            "SubagentStop",
            transcript_path=str(CLIENT_PATH),
            last_assistant_message="two findings",
        ),
        SENDER,
    )

    assert await subagent_runs_of(octomate) == []
    assert tailer.sessions == {}


async def test_subagent_stop_waits_for_the_answer_line_to_land() -> None:
    """The final answer line races the synchronous SubagentStop hook (measured live:
    it usually loses). The event names the answer, so the stop waits until the
    stream yields it — without this, the committed child run is final and
    permanently missing its own conclusion."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(tailer, state, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    # Everything except the final answer line has streamed when the hook fires.
    fed = await feed_records(tailer, state, SUB_TURN_ONE[:-1], agent_id=AGENT_ID)

    async def late_writer() -> None:
        await anyio.sleep(0.3)
        await feed_records(
            tailer, state, SUB_TURN_ONE[-1:], agent_id=AGENT_ID, start=fed
        )

    writer = asyncio.ensure_future(late_writer())
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings"), SENDER
    )
    await writer

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE)
    assert [type(m).__name__ for m in child_run.messages] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
    ]
    tailer.detach_remote(state)


async def test_subagent_stop_never_hangs_on_an_answer_that_never_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tailer_mod, "SUBAGENT_SETTLE_TIMEOUT", 0.3)
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(tailer, state, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    await feed_records(tailer, state, SUB_TURN_ONE[:-1], agent_id=AGENT_ID)

    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="words never written"),
        SENDER,
    )

    # Bounded: it committed what actually streamed rather than waiting forever.
    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE[:-1])
    tailer.detach_remote(state)


async def test_a_diverging_announced_answer_settles_on_quiescence() -> None:
    """No contract promises the hook's last_assistant_message equals the
    transcript's text byte-for-byte (truncation, block joins). A divergence must
    cost a couple of poll beats, not the full timeout — and never the commit."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed_records(tailer, state, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    await feed_records(tailer, state, SUB_TURN_ONE, agent_id=AGENT_ID)  # complete

    started = monotonic()
    await ingest.handle(
        subagent_hook(
            "SubagentStop", last_assistant_message="two findings… [truncated]"
        ),
        SENDER,
    )
    elapsed = monotonic() - started

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == total_bytes(SUB_TURN_ONE)
    # Quiescence (2 quiet polls ≈ 0.4s) settled it, not the 2s strict timeout.
    assert elapsed < 1.5
    tailer.detach_remote(state)
