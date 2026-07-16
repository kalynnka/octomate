from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agent.codex.hooks import CodexHookInput
from octomate.tentacles.agent.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.agent.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.agent.locks import SessionLocks

SESSION_ID = "codex-session"
TURN_ID = "codex-turn"


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def write_rollout(path: Path) -> None:
    records = [
        {
            "timestamp": "2026-07-16T10:00:00Z",
            "type": "session_meta",
            "payload": {"session_id": SESSION_ID, "originator": "codex_cli"},
        },
        {
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_ID},
        },
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect it"},
        },
        {
            "timestamp": "2026-07-16T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec",
                "input": "pwd",
            },
        },
        {
            "timestamp": "2026-07-16T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "repo",
            },
        },
        {
            "timestamp": "2026-07-16T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "message-1",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {
            "timestamp": "2026-07-16T10:00:06Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": TURN_ID},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def wired(octomate: Octomate) -> tuple[CodexHookIngest, CodexTranscriptTailer]:
    locks = SessionLocks()
    tailer = CodexTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    return CodexHookIngest(octomate, tailer, locks), tailer


async def test_hooks_sketch_then_rollout_replaces_with_full_turn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    common = {
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "transcript_path": path,
    }
    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "UserPromptSubmit", "prompt": "inspect it"}
        )
    )
    await ingest.handle(
        CodexHookInput.model_validate(
            {
                **common,
                "hook_event_name": "Stop",
                "last_assistant_message": "done",
            }
        )
    )

    conversation = await octomate.conversations.ensure(
        (
            await octomate.thread_manager.ensure(
                ThreadKey(CODEX_NATIVE_ID, "private", SESSION_ID, "")
            )
        ).id,
        agent_tentacle_id=CODEX_NATIVE_ID,
    )
    [sketch] = conversation.runs
    assert isinstance(sketch, ExternalAgentRun)
    assert sketch.end_offset is None

    write_rollout(path)
    await tailer.pump_session(SESSION_ID)

    conversation = await octomate.conversations.ensure(
        conversation.thread_id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    [run] = conversation.runs
    assert isinstance(run, ExternalAgentRun)
    assert run.id == TURN_ID
    assert run.end_offset == path.stat().st_size
    assert [message.message_text for message in run.messages] == [
        "inspect it",
        None,
        "done",
    ]
    await tailer.shutdown()


async def test_driven_session_hooks_are_ignored() -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    with ingest.driving(SESSION_ID):
        await ingest.handle(
            CodexHookInput(
                hook_event_name="UserPromptSubmit",
                session_id=SESSION_ID,
                turn_id=TURN_ID,
                prompt="do not ingest",
            )
        )

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "private", SESSION_ID, "")
    )
    assert thread.messages == []
    await tailer.shutdown()


async def test_marked_session_start_is_ignored_before_the_sdk_returns_its_id() -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        CodexHookInput(
            hook_event_name="SessionStart",
            session_id=SESSION_ID,
            octomate_driven=True,
        )
    )

    assert octomate.thread_manager.threads == {}
    await tailer.shutdown()


async def test_nested_task_does_not_close_or_pollute_the_parent(tmp_path: Path) -> None:
    path = tmp_path / "nested.jsonl"
    records = [
        {
            "timestamp": "2026-07-16T10:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_ID},
        },
        {
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "parent prompt"},
        },
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "child-turn"},
        },
        {
            "timestamp": "2026-07-16T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "child answer"}],
            },
        },
        {
            "timestamp": "2026-07-16T10:00:04Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "child-turn"},
        },
        {
            "timestamp": "2026-07-16T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "parent answer"}],
            },
        },
        {
            "timestamp": "2026-07-16T10:00:06Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": TURN_ID},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        CodexHookInput(
            hook_event_name="SessionStart",
            session_id=SESSION_ID,
            transcript_path=path,
        )
    )
    await tailer.pump_session(SESSION_ID)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "private", SESSION_ID, "")
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    [run] = conversation.runs
    assert run.id == TURN_ID
    assert [message.message_text for message in run.messages] == [
        "parent prompt",
        "parent answer",
    ]
    await tailer.shutdown()
