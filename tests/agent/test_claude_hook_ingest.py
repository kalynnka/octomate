"""UoW-A — live human-ledger ingest of a native Claude Code session's hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelMessage
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest

SESSION_ID = "sess-1"
SESSION_KEY = ThreadKey(CLAUDE_NATIVE_ID, "private", SESSION_ID, "")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def hook(name: str, prompt_id: str | None = None, **body: JsonValue) -> ClaudeHookInput:
    """The event as FastAPI would validate it from the POST body — extra event-specific
    keys ride in `body` and are ignored unless modeled."""
    return ClaudeHookInput.model_validate(
        {
            "hook_event_name": name,
            "session_id": SESSION_ID,
            "cwd": "/repo",
            "transcript_path": f"/x/{SESSION_ID}.jsonl",
            **({"prompt_id": prompt_id} if prompt_id is not None else {}),
            **body,
        }
    )


async def submit(ingest: ClaudeHookIngest, prompt_id: str, prompt: str) -> None:
    await ingest.handle(hook("UserPromptSubmit", prompt_id, prompt=prompt))


async def stop(ingest: ClaudeHookIngest, prompt_id: str, answer: str) -> None:
    await ingest.handle(
        hook("Stop", prompt_id, stop_hook_active=False, last_assistant_message=answer)
    )


async def ledger(octomate: Octomate) -> list[tuple[str, str | None, str | None]]:
    """The thread's chat log as (direction, platform_message_id, text)."""
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    return [
        (m.direction, m.platform_message_id, m.message_text) for m in thread.messages
    ]


async def test_a_turn_writes_inbound_and_outbound_tagged_by_prompt_id() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "list the files")
    await stop(ingest, "p1", "Here are the files.")

    assert await ledger(octomate) == [
        ("inbound", "p1", "list the files"),
        ("outbound", "p1", "Here are the files."),
    ]


async def test_multiple_turns_accumulate_in_order() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "first")
    await stop(ingest, "p1", "first done")
    await submit(ingest, "p2", "second")
    await stop(ingest, "p2", "second done")

    assert await ledger(octomate) == [
        ("inbound", "p1", "first"),
        ("outbound", "p1", "first done"),
        ("inbound", "p2", "second"),
        ("outbound", "p2", "second done"),
    ]


async def test_refiring_events_is_idempotent() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "list the files")
    await submit(ingest, "p1", "list the files")  # retry
    await stop(ingest, "p1", "done")
    await stop(ingest, "p1", "done")  # a repeated Stop

    assert await ledger(octomate) == [
        ("inbound", "p1", "list the files"),
        ("outbound", "p1", "done"),
    ]


async def test_crash_before_stop_leaves_a_clean_inbound_only_turn() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "do a thing")
    # no Stop — the session died mid-turn

    assert await ledger(octomate) == [("inbound", "p1", "do a thing")]


async def test_no_model_frame_is_written_live() -> None:
    """The human ledger is the whole live footprint: no conversation, run, or model
    message — those are rebuilt from the transcript on restore (UoW-B)."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "hello")
    await stop(ingest, "p1", "hi")

    async with async_session() as session:
        conversations = await session.list(Conversation, limit=None, order_bys=[])
        runs = await session.list(AgentRun, limit=None, order_bys=[])
        messages = await session.list(ModelMessage, limit=None, order_bys=[])
    assert conversations == []
    assert runs == []
    assert messages == []


async def test_empty_prompt_and_empty_answer_are_skipped() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt=""))
    await ingest.handle(hook("Stop", "p1", last_assistant_message=""))

    assert await ledger(octomate) == []


async def test_session_end_releases_the_lock() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await submit(ingest, "p1", "hello")
    assert SESSION_ID in ingest.locks
    await ingest.handle(hook("SessionEnd", reason="other"))
    assert SESSION_ID not in ingest.locks


async def test_unhandled_events_are_ignored() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate)

    await ingest.handle(
        hook("PreToolUse", "p1", tool_name="Bash", tool_input={"command": "ls"})
    )
    await ingest.handle(hook("MessageDisplay", "p1", delta="thinking...", final=False))

    assert await ledger(octomate) == []
