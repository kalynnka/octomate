"""UoW-B — rebuild a native Claude session's full model timeline from its transcript."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.messages import ModelResponse
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import MessageBinding, ThreadKey
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agent.claude.restore import ClaudeSessionRestore
from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer
from octomate.types.json import JsonObject

SESSION_ID = "sess-1"
SESSION_KEY = ThreadKey(CLAUDE_NATIVE_ID, "private", SESSION_ID, "")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def prompt_record(prompt_id: str, text: str, second: int) -> JsonObject:
    """A submitted prompt, padded the way Claude Code pads it — the injected editor
    context rides as an extra text block ahead of what the human typed."""
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
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>ignore</system-reminder>"},
                {"type": "text", "text": text},
            ],
        },
    }


def assistant_record(
    uid: str, second: int, content: list[JsonValue], stop_reason: str
) -> JsonObject:
    message: JsonObject = {
        "id": f"msg-{uid}",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-8",
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "content": content,
    }
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
        "message": message,
    }


def tool_result_record(prompt_id: str, uid: str, second: int) -> JsonObject:
    """A tool result — carries promptId but no promptSource, the only thing telling it
    apart from a real prompt."""
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


def write_transcript(path: Path) -> None:
    records: list[JsonObject] = [
        prompt_record("p1", "list the files", 1),
        assistant_record(
            "a1",
            2,
            [
                {"type": "thinking", "thinking": "let me look", "signature": "sig-xyz"},
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
            "tool_use",
        ),
        tool_result_record("p1", "a1", 3),
        assistant_record("a2", 4, [{"type": "text", "text": "Done."}], "end_turn"),
        # A sub-agent's line interleaves into the same file; it must not land here.
        {
            "type": "assistant",
            "isSidechain": True,
            "userType": "external",
            "cwd": "/repo",
            "sessionId": SESSION_ID,
            "version": "2.1.0",
            "gitBranch": "main",
            "parentUuid": None,
            "entrypoint": "cli",
            "uuid": "side",
            "timestamp": "2026-07-09T10:00:05.000Z",
            "message": {
                "id": "msg-side",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": "subagent chatter"}],
            },
        },
        prompt_record("p2", "now commit", 6),
        assistant_record("a3", 7, [{"type": "text", "text": "Committed."}], "end_turn"),
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


async def conversation_runs(octomate: Octomate) -> list[str]:
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    return [run.id for run in conversation.runs]


async def test_rebuild_reconstructs_full_fidelity(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()
    restore = ClaudeSessionRestore(octomate)

    outcome = await restore.restore(SESSION_ID, transcript)

    assert outcome.status == "done"
    assert outcome.recorded_run_ids == ["p1", "p2"]

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    first = next(run for run in conversation.runs if run.id == "p1")
    # A native run reads back as the external variant, carrying its session + source.
    assert isinstance(first, ExternalAgentRun)
    assert first.external_session_id == SESSION_ID
    assert first.source == "cli"  # the transcript entrypoint

    kinds = [type(message).__name__ for message in first.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]

    # The response carries the thinking + tool call, with model + usage the events
    # never had.
    response = first.messages[1]
    assert isinstance(response, ModelResponse)
    part_kinds = [type(part).__name__ for part in response.parts]
    assert "ThinkingPart" in part_kinds
    assert "ToolCallPart" in part_kinds
    assert response.model_name == "claude-opus-4-8"
    assert response.usage.output_tokens == 7

    thinking = next(p for p in response.parts if type(p).__name__ == "ThinkingPart")
    assert thinking.signature == "sig-xyz"  # pyright: ignore[reportAttributeAccessIssue]

    # Dated by the transcript, not by rebuild wall-clock; sub-agent line excluded.
    assert first.started_at is not None
    assert first.started_at.isoformat().startswith("2026-07-09T10:00:01")
    assert "subagent chatter" not in json.dumps(
        [message.message_text for message in conversation.messages]
    )


async def test_rebuild_binds_and_creates_the_human_ledger(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()

    # No live ingest ran, so restore must create the human ledger from the transcript.
    await ClaudeSessionRestore(octomate).restore(SESSION_ID, transcript)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    texts = [(m.direction, m.message_text) for m in thread.messages]
    # Restore had no live UserPromptSubmit, so the inbound prompt is the padded
    # transcript copy (injected context + the human's words); the outbound answer
    # comes clean from the assistant's own text.
    assert [direction for direction, _ in texts] == [
        "inbound",
        "outbound",
        "inbound",
        "outbound",
    ]
    assert texts[0][1] is not None and "list the files" in texts[0][1]
    assert texts[1] == ("outbound", "Done.")
    assert texts[2][1] is not None and "now commit" in texts[2][1]
    assert texts[3] == ("outbound", "Committed.")
    async with async_session() as session:
        bindings = await session.list(MessageBinding, limit=None, order_bys=[])
    assert sorted({binding.kind for binding in bindings}) == [
        "assistant_reply",
        "request_source",
    ]


async def test_rebuild_reuses_the_live_ledger_rows(tmp_path: Path) -> None:
    """When UoW-A already wrote the human ledger, restore binds those rows rather than
    creating duplicates."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()

    ingest = ClaudeHookIngest(
        octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    )
    await ingest.record_prompt(_hook("p1", prompt="list the files"), "list the files")
    await ingest.record_answer(_hook("p1"), "Done.")

    await ClaudeSessionRestore(octomate).restore(SESSION_ID, transcript)

    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    inbound_p1 = [
        m
        for m in thread.messages
        if m.platform_message_id == "p1" and m.direction == "inbound"
    ]
    assert len(inbound_p1) == 1  # bound the existing row, did not duplicate it


async def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()
    restore = ClaudeSessionRestore(octomate)

    await restore.restore(SESSION_ID, transcript)
    second = await restore.restore(SESSION_ID, transcript)

    assert second.recorded_run_ids == []  # nothing new the second time
    assert await conversation_runs(octomate) == ["p1", "p2"]
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    assert len(thread.messages) == 4  # no duplicate ledger rows


async def test_concurrent_restores_dedup_to_one_rebuild(tmp_path: Path) -> None:
    """A web open landing while the fire-and-forget rebuild runs joins it — the
    transcript is parsed once, not twice."""
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()
    restore = ClaudeSessionRestore(octomate)

    calls = 0
    real_rebuild = restore.rebuild

    async def counting_rebuild(session_id: str, path: Path) -> list[str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)  # hold the task open so the second call overlaps
        return await real_rebuild(session_id, path)

    restore.rebuild = counting_rebuild  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        restore.restore(SESSION_ID, transcript),
        restore.restore(SESSION_ID, transcript),
    )

    assert calls == 1  # deduped: one rebuild served both callers
    assert first is second
    assert restore.status(SESSION_ID) == "done"
    assert await conversation_runs(octomate) == ["p1", "p2"]


async def test_status_tracks_the_lifecycle(tmp_path: Path) -> None:
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    write_transcript(transcript)
    octomate = Octomate()
    restore = ClaudeSessionRestore(octomate)

    assert restore.status(SESSION_ID) is None
    task = restore.task(SESSION_ID, transcript)
    assert restore.status(SESSION_ID) == "running"
    await task
    assert restore.status(SESSION_ID) == "done"


async def test_rebuild_failure_is_captured_not_raised(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    octomate = Octomate()
    restore = ClaudeSessionRestore(octomate)

    outcome = await restore.restore(SESSION_ID, missing)

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert restore.status(SESSION_ID) == "failed"


def _hook(prompt_id: str, **body: JsonValue):
    from octomate.tentacles.agent.claude.hooks import ClaudeHookInput

    return ClaudeHookInput.model_validate(
        {
            "hook_event_name": "x",
            "session_id": SESSION_ID,
            "prompt_id": prompt_id,
            **body,
        }
    )
