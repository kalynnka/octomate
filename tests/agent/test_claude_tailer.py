"""UoW-A — the offset tailer: follow a native session's transcript as it is written,
recording an `ExternalAgentRun` per turn (with its byte range) and streaming live
events, checkpointed so an interrupted session resumes without dup or loss."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.events import StreamEvents
from octomate.database import async_session
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import MessageBinding, ThreadKey
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agent.claude.locks import SessionLocks
from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer, TailState
from octomate.types.json import JsonObject

SESSION_ID = "sess-tail"
SESSION_KEY = ThreadKey(CLAUDE_NATIVE_ID, "private", SESSION_ID, "")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


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


def write_records(path: Path, records: list[JsonObject]) -> None:
    path.write_bytes(b"".join(line_bytes(record) for record in records))


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


async def test_records_runs_with_byte_ranges(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # The whole file is already on disk, so the catch-up pump assembles both turns;
    # finalize drains to EOF and commits the still-open trailing turn.
    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]

    p1, p2 = runs
    assert p1.source == "cli"  # the transcript entrypoint
    assert p1.last_line_uuid == "a2"  # last line folded into turn one
    # Contiguous, gapless byte ranges over the file: p1 ends where p2 begins.
    assert p1.start_offset == 0
    assert p1.end_offset == p2.start_offset
    assert p2.end_offset == transcript.stat().st_size

    kinds = [type(message).__name__ for message in p1.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]


async def test_streams_live_events_to_a_consumer(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    state = tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    events = drain(state)
    # The turn's thinking / tool call / result / answer surface as stream events.
    kinds = {type(event).__name__ for event in events}
    assert "FunctionToolCallEvent" in kinds
    assert "FunctionToolResultEvent" in kinds


async def test_no_consumer_still_records_every_run(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # Nobody reads the live stream; drop-on-full must never stall the durable sink.
    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_malformed_line_does_not_stall_the_cursor(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    records = TURN_ONE + TURN_TWO
    # Splice a garbage (unparseable) line in right after turn one's prompt.
    head = line_bytes(records[0])
    garbage = b'{"type": "assistant", "broken\n'
    rest = b"".join(line_bytes(record) for record in records[1:])
    transcript.write_bytes(head + garbage + rest)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    # The bad line is skipped, the cursor advances past it, and both turns still record.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_resume_from_offset_after_interruption(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # First run sees only turn one, then the process "dies" (finalize commits p1).
    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)
    (p1,) = await runs_of(octomate)
    assert p1.id == "p1"

    # Turn two is appended while nobody watched; resume from where p1 ended.
    with transcript.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in TURN_TWO))
    tailer.start(SESSION_ID, transcript, offset=p1.end_offset or 0)
    await tailer.finalize(SESSION_ID)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]
    assert runs[1].start_offset == p1.end_offset  # no gap, no overlap


async def test_rerun_over_committed_bytes_is_idempotent(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)
    # Re-tail the whole file from zero: every turn is already recorded, so re-reading its
    # bytes commits nothing new (dedup by prompt_id).
    tailer.start(SESSION_ID, transcript, offset=0)
    await tailer.finalize(SESSION_ID)

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_live_append_is_picked_up_by_the_watch(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await anyio.sleep(0.1)  # let the catch-up pump open turn one (still uncommitted)

    # Appending turn two's prompt closes turn one — the directory watch must wake, pump,
    # and commit it, with no finalize in between.
    with transcript.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in TURN_TWO))

    with anyio.fail_after(5):
        while not any(run.id == "p1" for run in await runs_of(octomate)):
            await anyio.sleep(0.05)

    await tailer.finalize(SESSION_ID)
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


async def test_full_lifecycle_records_runs_and_binds_the_ledger(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    ingest, _ = wired(octomate)

    # Turn one: the first prompt starts the tailer (no SessionStart on http hooks); the
    # hooks write its human ledger.
    await ingest.handle(
        hook_event("UserPromptSubmit", "p1", transcript, prompt="list the files")
    )
    await ingest.handle(hook_event("Stop", "p1", last_assistant_message="Done."))
    # Turn two arrives, then ends the session — finalize drains the whole file.
    with transcript.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in TURN_TWO))
    await ingest.handle(
        hook_event("UserPromptSubmit", "p2", transcript, prompt="now commit")
    )
    await ingest.handle(hook_event("Stop", "p2", last_assistant_message="Committed."))
    await ingest.handle(hook_event("SessionEnd", transcript=transcript, reason="other"))

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


async def test_shutdown_cancels_the_follow_loop(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    state = tailer.start(SESSION_ID, transcript)
    await anyio.sleep(0.05)  # let the loop reach its directory watch
    assert tailer.is_following(SESSION_ID)

    await tailer.shutdown()

    assert not tailer.is_following(SESSION_ID)
    assert tailer.sessions == {}
    assert state.task is not None and state.task.done()


async def test_line_split_across_pumps_is_not_half_parsed(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    state = tailer.new_state(SESSION_ID, transcript)
    await tailer.prepare(state)

    prompt = line_bytes(prompt_record("p1", "hello", 1))
    # Write the prompt line without its newline: a bare fragment must not be parsed.
    transcript.write_bytes(prompt[:-1])
    await tailer.pump(state)
    assert state.open_turn is None
    assert state.offset == 0

    # Complete the line and append the answer; now both frame and process.
    with transcript.open("ab") as handle:
        handle.write(b"\n" + line_bytes(assistant_record("a1", 2, [{"type": "text", "text": "hi"}])))
    await tailer.pump(state)
    assert state.open_turn is not None
    assert state.open_turn.prompt_id == "p1"

    await tailer.close_turn(state)
    assert [run.id for run in await runs_of(octomate)] == ["p1"]
