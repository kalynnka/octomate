"""UoW-A — the offset tailer: follow a native session's transcript as it is written,
recording an `ExternalAgentRun` per turn (with its byte range) and streaming live
events, checkpointed so an interrupted session resumes without dup or loss."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.events import StreamEvents
from octomate.database import async_session
from octomate.schemas.messages import ModelResponse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import MessageBinding, ThreadKey
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agent.claude import tailer as tailer_mod
from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer, TailState
from octomate.tentacles.agent.locks import SessionLocks
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


def wired(
    octomate: Octomate, roots: tuple[Path, ...] = ()
) -> tuple[ClaudeHookIngest, ClaudeTranscriptTailer]:
    """The ingest + tailer as the tentacle wires them: sharing one per-session lock
    registry so hook ledger writes and tailer run commits serialize. Tests that
    expect a hook-claimed path to be tailed add the tmp tree they write into — the
    same injection the tentacle does from `agents.claude.transcript_root`."""
    locks = SessionLocks()
    tailer = ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    return ClaudeHookIngest(
        octomate, tailer, locks, extra_transcript_roots=roots
    ), tailer


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


async def test_the_tailer_supersedes_the_hooks_sketch_of_a_turn(tmp_path: Path) -> None:
    """One run per turn across both tiers: the hooks sketch it live from what they see,
    and closing the turn supersedes that with the transcript's full timeline. `prompt_id`
    is the run id both write under, so the sketch is upgraded, never duplicated."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    ingest, _ = wired(octomate)

    await ingest.handle(
        hook_event("UserPromptSubmit", "p1", transcript, prompt="list the files")
    )
    await ingest.handle(hook_event("Stop", "p1", last_assistant_message="Done."))

    # In flight: the prompt and the answer, and no byte range — the mark of a sketch.
    [sketch] = await runs_of(octomate)
    assert sketch.id == "p1"
    assert [message.message_text for message in sketch.messages] == [
        "list the files",
        "Done.",
    ]
    assert sketch.end_offset is None

    await ingest.handle(hook_event("SessionEnd", transcript=transcript, reason="other"))

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


async def test_relocation_follows_the_moved_transcript(tmp_path: Path) -> None:
    dir_a = tmp_path / "slug-a"
    dir_b = tmp_path / "slug-b"
    dir_a.mkdir()
    dir_b.mkdir()
    path_a = dir_a / f"{SESSION_ID}.jsonl"
    write_records(path_a, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    state_a = tailer.start(SESSION_ID, path_a)
    await anyio.sleep(0.1)  # catch up turn one (p1 open, uncommitted)

    # The session's slug moves: the same bytes relocate to a new dir, and turn two lands.
    path_b = dir_b / f"{SESSION_ID}.jsonl"
    path_b.write_bytes(path_a.read_bytes())
    with path_b.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in TURN_TWO))

    state_b = tailer.start(SESSION_ID, path_b)
    assert state_b is not state_a
    assert state_b.transcript_path == path_b
    await tailer.finalize(SESSION_ID)

    # The uncommitted turn one is re-read from the moved file, so nothing is lost.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_idle_loop_self_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tailer_mod, "IDLE_TIMEOUT", 0.1)
    monkeypatch.setattr(tailer_mod, "IDLE_POLL_MS", 50)
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # A session that never sends SessionEnd: no new bytes ever arrive, so past the idle
    # window the loop self-finalizes — commits the trailing turn and drops itself.
    tailer.start(SESSION_ID, transcript)
    with anyio.fail_after(5):
        while tailer.is_following(SESSION_ID):
            await anyio.sleep(0.05)

    assert tailer.sessions == {}
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_a_commit_that_times_out_stops_the_tail_rather_than_skipping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that cannot commit must stop the tail. Skipping it and carrying on would
    let p2 commit with a higher `end_offset`, pushing the resume mark past p1's bytes and
    stranding p1 where no recovery could reach it."""
    monkeypatch.setattr(tailer_mod, "COMMIT_LOCK_TIMEOUT", 0.05)
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    locks = SessionLocks()
    tailer = ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )

    async with locks.hold(SESSION_ID):  # a wedged holder the commit can't get past
        tailer.start(SESSION_ID, transcript)
        with anyio.fail_after(5):
            while tailer.is_following(SESSION_ID):
                await anyio.sleep(0.02)

    assert await runs_of(octomate) == []  # p2 did not commit over p1's failure

    # So the resume mark still points at p1's bytes, and recovery reaches both turns.
    assert await tailer.recover(SESSION_ID, transcript) == ["p1", "p2"]


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
        handle.write(
            b"\n"
            + line_bytes(assistant_record("a1", 2, [{"type": "text", "text": "hi"}]))
        )
    await tailer.pump(state)
    assert state.open_turn is not None
    assert state.open_turn.prompt_id == "p1"

    await tailer.close_turn(state)
    assert [run.id for run in await runs_of(octomate)] == ["p1"]


# --- recover(): the manual on-demand rebuild, reusing the same assembly ---


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


async def test_recover_reconstructs_full_fidelity(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + [sidechain_record("side", 5)] + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    recovered = await tailer.recover(SESSION_ID, transcript)
    assert recovered == ["p1", "p2"]

    first = (await runs_of(octomate))[0]
    assert first.id == "p1"
    assert first.external_session_id == SESSION_ID
    assert first.source == "cli"
    kinds = [type(message).__name__ for message in first.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]

    response = first.messages[1]
    assert isinstance(response, ModelResponse)
    part_kinds = [type(part).__name__ for part in response.parts]
    assert "ThinkingPart" in part_kinds and "ToolCallPart" in part_kinds
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


async def test_recover_creates_the_human_ledger(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # No hooks ever ran, so recover creates the ledger from the transcript and binds it.
    await tailer.recover(SESSION_ID, transcript)

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


async def test_recover_reuses_live_ledger_rows(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # The hooks already wrote p1's ledger; recover binds those rows, not duplicates.
    ingest = ClaudeHookIngest(octomate, tailer)
    await ingest.record_prompt(hook("p1", prompt="list the files"), "list the files")
    await ingest.record_answer(hook("p1"), "Done.")

    await tailer.recover(SESSION_ID, transcript)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    inbound_p1 = [
        m
        for m in thread.messages
        if m.platform_message_id == "p1" and m.direction == "inbound"
    ]
    assert len(inbound_p1) == 1  # bound the existing row, did not duplicate it


async def test_recover_is_idempotent(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await tailer.recover(SESSION_ID, transcript)
    again = await tailer.recover(SESSION_ID, transcript)

    assert again == []  # nothing new the second time
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    assert len(thread.messages) == 4  # no duplicate ledger rows


async def test_recover_resumes_from_the_last_committed_offset(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    # First recovery sees only turn one.
    assert await tailer.recover(SESSION_ID, transcript) == ["p1"]
    (p1,) = await runs_of(octomate)

    # Turn two lands later; recovery resumes at max(end_offset), reproducing it exactly.
    with transcript.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in TURN_TWO))
    assert await tailer.recover(SESSION_ID, transcript) == ["p2"]

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]
    assert runs[1].start_offset == p1.end_offset  # resumed exactly at the boundary


async def test_recover_missing_transcript_is_a_noop(tmp_path: Path) -> None:
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    assert await tailer.recover(SESSION_ID, tmp_path / "gone.jsonl") == []


async def test_session_end_recovers_a_session_no_loop_followed(tmp_path: Path) -> None:
    """Octomate restarted mid-session, so nothing is following when SessionEnd lands —
    the registry it would have ended is gone. The hook is the session's last word, so it
    rebuilds the tail from the transcript rather than dropping it."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    assert not tailer.is_following(SESSION_ID)
    await ingest.handle(hook_event("SessionEnd", transcript=transcript, reason="other"))

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_recover_overlapping_a_live_follow_leaves_it_tailing(
    tmp_path: Path,
) -> None:
    """The durable sink — not the follow loop's in-memory guard — is what makes a
    re-commit a no-op. A recovery racing a live loop commits a turn the loop still holds
    open, so the loop's own commit of it must be a no-op rather than a primary-key
    collision: that error escapes into `follow`'s blanket handler, which ends the loop
    and silently strands every later turn of the session."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE + TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await anyio.sleep(0.1)  # the loop commits p1, and holds p2 open at EOF
    # A second reader resumes past p1 and commits p2 — the turn the loop still holds.
    assert await tailer.recover(SESSION_ID, transcript) == ["p2"]

    # A later turn lands: closing p2 re-commits it, over bytes recover already wrote.
    turn_three = [
        prompt_record("p3", "and push", 9),
        assistant_record("a4", 10, [{"type": "text", "text": "Pushed."}]),
    ]
    with transcript.open("ab") as handle:
        handle.write(b"".join(line_bytes(record) for record in turn_three))
    await tailer.finalize(SESSION_ID)

    # p3 is the proof: the loop lived through re-committing p2 and kept tailing.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2", "p3"]


async def test_a_backfilled_row_is_dated_by_the_transcript_not_the_replay(
    tmp_path: Path,
) -> None:
    """A session Octomate only saw after the fact: no hook wrote its ledger, so the
    tailer creates those rows itself from the transcript.

    Such a row is history, and the transcript records when the turn really happened —
    `2026-07-09`, days before this replay. Dating it `now` would be a lie the ledger
    keeps, so the transcript's clock is what a backfilled row carries.
    """
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, TURN_ONE)
    octomate = Octomate()
    ingest, _ = wired(octomate)

    # SessionEnd alone: the live tier never saw this session's prompt or answer.
    await ingest.handle(hook_event("SessionEnd", transcript=transcript, reason="other"))

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated  # the tailer did write the ledger

    # The prompt line says 10:00:01, the final assistant line 10:00:04.
    assert dated["inbound"] == datetime(2026, 7, 9, 10, 0, 1, tzinfo=timezone.utc)
    assert dated["outbound"] == datetime(2026, 7, 9, 10, 0, 4, tzinfo=timezone.utc)


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


def write_subagent(tmp_path: Path, records: list[JsonObject]) -> Path:
    directory = tmp_path / SESSION_ID / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{AGENT_ID}.jsonl"
    path.write_bytes(b"".join(line_bytes(record) for record in records))
    return path


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


async def test_a_subagent_transcript_becomes_a_child_run(tmp_path: Path) -> None:
    """The subagent's own file is ingested as a child run in its own conversation:
    linked to the parent turn (parent_run_id) and the spawning call
    (parent_tool_call_id), keyed so it can never collide with the parent run, and
    absent from the human ledger — a subagent has no prompt or answer of its own."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    write_records(transcript, turn_one + TURN_TWO)
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    # The parent's own turns are untouched by the child's presence.
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.parent_run_id == "p1"
    assert child_run.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"
    assert child_run.external_session_id == AGENT_ID
    assert child_run.start_offset == 0
    assert child_run.end_offset == sub_path.stat().st_size
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


async def test_a_resumed_subagent_adds_a_second_run_to_one_conversation(
    tmp_path: Path,
) -> None:
    """4 of 93 measured subagent transcripts carry more than one promptId: the
    subagent was resumed by a later parent turn and kept its history. One child
    conversation, one run per parent turn — and the second turn opens on a
    tool-result line, which must still open it."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    write_records(transcript, turn_one + TURN_TWO)
    write_subagent(tmp_path, SUB_TURN_ONE + SUB_TURN_TWO)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)

    first, second = await subagent_runs_of(octomate)
    assert first.id == f"{AGENT_ID}:p1"
    assert second.id == f"{AGENT_ID}:p2"
    assert (first.parent_run_id, second.parent_run_id) == ("p1", "p2")
    # Contiguous child byte ranges: the resumed turn starts where the first ended.
    assert first.end_offset == second.start_offset
    assert second.messages, "a tool-result opener still yields a timeline"


async def test_retailing_a_session_reproduces_the_same_subagent_tree(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    write_records(transcript, turn_one)
    write_subagent(tmp_path, SUB_TURN_ONE)
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    tailer.start(SESSION_ID, transcript)
    await tailer.finalize(SESSION_ID)
    before = [(run.id, run.end_offset) for run in await subagent_runs_of(octomate)]

    # Recover over the same bytes: idempotent, no duplicate child runs.
    await tailer.recover(SESSION_ID, transcript)
    after = [(run.id, run.end_offset) for run in await subagent_runs_of(octomate)]
    assert before == after == [(f"{AGENT_ID}:p1", None)] or before == after
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


async def test_subagent_stop_commits_the_child_run_without_waiting(
    tmp_path: Path,
) -> None:
    """The hooks are the child's live close signal: after SubagentStop the child run
    is durable — with its byte range and both parent links — while the session is
    still being followed and the parent's own turn is still open."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    # Parent turn p1 is still in flight: prompt + spawn, no closing prompt after.
    write_records(transcript, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE)
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))

    await ingest.handle(
        subagent_hook(
            "SubagentStart",
            transcript_path=str(transcript),
            prompt="audit the repo",
        )
    )
    assert tailer.is_following(SESSION_ID)  # start self-healed the session tail
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings")
    )

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.end_offset == sub_path.stat().st_size
    assert child_run.parent_run_id == "p1"
    assert child_run.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"
    # The parent session is still live and its own turn has not been committed.
    assert tailer.is_following(SESSION_ID)
    assert [run.id for run in await runs_of(octomate)] == []
    await tailer.finalize(SESSION_ID)


async def test_subagent_start_sketches_the_child_run_when_keyed(tmp_path: Path) -> None:
    """With a prompt_id on the event, the child run exists from SubagentStart — a
    dated, provisional sketch the tailer later replaces under the same id."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    (tmp_path / SESSION_ID / "subagents").mkdir(parents=True)
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))

    before = datetime.now(timezone.utc)
    await ingest.handle(
        subagent_hook(
            "SubagentStart",
            transcript_path=str(transcript),
            prompt_id="p1",
            prompt="audit the repo",
        )
    )
    after = datetime.now(timezone.utc)

    [sketch] = await subagent_runs_of(octomate)
    assert sketch.id == f"{AGENT_ID}:p1"
    assert sketch.end_offset is None  # provisional: no byte range yet
    assert sketch.parent_run_id == "p1"
    assert sketch.started_at is not None and before <= sketch.started_at <= after
    assert [message.message_text for message in sketch.messages] == ["audit the repo"]

    # The child's file lands and the subagent stops: the full timeline replaces the
    # sketch as the same run, now final.
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE)
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings")
    )
    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.end_offset == sub_path.stat().st_size
    assert len(child_run.messages) == 4
    await tailer.finalize(SESSION_ID)


async def test_an_event_carrying_agent_id_never_touches_the_parent_turn(
    tmp_path: Path,
) -> None:
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
        )
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
        )
    )

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    assert thread.messages == []  # no inbound, no outbound: the ledger is the human's
    assert await runs_of(octomate) == []
    assert not tailer.is_following(SESSION_ID)


async def test_a_driven_sessions_subagent_hooks_are_suppressed(tmp_path: Path) -> None:
    """A subagent event carries the parent's session id, so the driving claim covers
    it for free — pinned here rather than trusted."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    with ingest.driving(SESSION_ID):
        await ingest.handle(
            subagent_hook("SubagentStart", prompt_id="p1", prompt="child work")
        )
    assert await subagent_runs_of(octomate) == []
    assert not tailer.is_following(SESSION_ID)


async def test_subagent_stop_with_nothing_following_recovers_the_session(
    tmp_path: Path,
) -> None:
    """Octomate came up mid-session: the stop is the child's last word, so the whole
    session — parent turns and subagent — is rebuilt from disk, even when the event
    names the child's own transcript file rather than the session's."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    turn_one = [TURN_ONE[0], *agent_spawn_records("p1", 2), TURN_ONE[3]]
    write_records(transcript, turn_one + TURN_TWO)
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE)
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    assert not tailer.is_following(SESSION_ID)
    await ingest.handle(
        subagent_hook(
            "SubagentStop",
            transcript_path=str(sub_path),
            last_assistant_message="two findings",
        )
    )

    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]
    [child_run] = await subagent_runs_of(octomate)
    assert child_run.id == f"{AGENT_ID}:p1"
    assert child_run.end_offset == sub_path.stat().st_size


async def test_subagent_stop_waits_for_the_answer_line_to_land(tmp_path: Path) -> None:
    """The final answer line races the synchronous SubagentStop hook (measured live:
    it usually loses). The event names the answer, so the stop drains until the file
    yields it — without this, the committed child run is final and permanently
    missing its own conclusion."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    # Everything except the final answer line is on disk when the hook fires.
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE[:-1])
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))
    await ingest.handle(subagent_hook("SubagentStart", transcript_path=str(transcript)))

    async def late_writer() -> None:
        await anyio.sleep(0.3)
        with sub_path.open("ab") as handle:
            handle.write(line_bytes(SUB_TURN_ONE[-1]))

    writer = asyncio.ensure_future(late_writer())
    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="two findings")
    )
    await writer

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == sub_path.stat().st_size
    assert [type(m).__name__ for m in child_run.messages] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
    ]
    await tailer.finalize(SESSION_ID)


async def test_subagent_stop_never_hangs_on_an_answer_that_never_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tailer_mod, "SUBAGENT_SETTLE_TIMEOUT", 0.3)
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE[:-1])
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))
    await ingest.handle(subagent_hook("SubagentStart", transcript_path=str(transcript)))

    await ingest.handle(
        subagent_hook("SubagentStop", last_assistant_message="words never written")
    )

    # Bounded: it committed what the file actually held rather than waiting forever.
    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == sub_path.stat().st_size
    await tailer.finalize(SESSION_ID)


async def test_a_diverging_announced_answer_settles_on_quiescence(
    tmp_path: Path,
) -> None:
    """No contract promises the hook's last_assistant_message equals the
    transcript's text byte-for-byte (truncation, block joins). A divergence must
    cost a couple of poll beats, not the full timeout — and never the commit."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_records(transcript, [TURN_ONE[0], *agent_spawn_records("p1", 2)])
    sub_path = write_subagent(tmp_path, SUB_TURN_ONE)  # complete file on disk
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))
    await ingest.handle(subagent_hook("SubagentStart", transcript_path=str(transcript)))

    from time import monotonic as now

    started = now()
    await ingest.handle(
        subagent_hook(
            "SubagentStop", last_assistant_message="two findings… [truncated]"
        )
    )
    elapsed = now() - started

    [child_run] = await subagent_runs_of(octomate)
    assert child_run.end_offset == sub_path.stat().st_size
    # Quiescence (2 quiet polls ≈ 0.4s) settled it, not the 2s strict timeout.
    assert elapsed < 1.5
    await tailer.finalize(SESSION_ID)
