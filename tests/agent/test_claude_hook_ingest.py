"""UoW-A — live human-ledger ingest of a native Claude Code session's hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer

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
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await submit(ingest, "p1", "list the files")
    await stop(ingest, "p1", "Here are the files.")

    assert await ledger(octomate) == [
        ("inbound", "p1", "list the files"),
        ("outbound", "p1", "Here are the files."),
    ]


async def test_multiple_turns_accumulate_in_order() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

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
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

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
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await submit(ingest, "p1", "do a thing")
    # no Stop — the session died mid-turn

    assert await ledger(octomate) == [("inbound", "p1", "do a thing")]


async def test_hooks_sketch_the_turns_run_live() -> None:
    """The hooks alone leave a whole conversation → run → messages chain for the turn,
    so a turn in flight has a model history to hang from before the transcript's real
    timeline lands. The sketch carries no byte range: that is what marks it provisional
    and lets the tailer replace it."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await submit(ingest, "p1", "hello")

    # The prompt alone already hangs off a run, keyed by the turn's prompt_id.
    assert await sketched(octomate) == [("p1", ["hello"])]

    await stop(ingest, "p1", "hi")

    # Stop carries no prompt, so the answer joins the prompt read back off the ledger.
    assert await sketched(octomate) == [("p1", ["hello", "hi"])]
    async with async_session() as session:
        runs = await session.list(AgentRun, limit=None, order_bys=[])
    assert [(run.start_offset, run.end_offset) for run in runs] == [(None, None)]


async def test_a_sketch_is_dated_so_it_sorts_after_the_history() -> None:
    """`Conversation.runs` and `.messages` both order on `started_at`, which is read off
    the run's first message — and `ModelRequest.timestamp` defaults to None. An undated
    sketch would sort ahead of every turn before it, putting the live prompt at the head
    of the history it belongs at the end of."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await submit(ingest, "p1", "first")
    await stop(ingest, "p1", "done")
    await submit(ingest, "p2", "second")  # the turn now in flight

    assert [run_id for run_id, _ in await sketched(octomate)] == ["p1", "p2"]
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    assert all(run.started_at is not None for run in conversation.runs)
    assert [message.message_text for message in conversation.messages] == [
        "first",
        "done",
        "second",
    ]


async def sketched(octomate: Octomate) -> list[tuple[str, list[str | None]]]:
    """Each run of the session's conversation as (run_id, its messages' text)."""
    thread = await octomate.thread_manager.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    return [
        (run.id, [message.message_text for message in run.messages])
        for run in conversation.runs
    ]


async def test_empty_prompt_and_empty_answer_are_skipped() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt=""))
    await ingest.handle(hook("Stop", "p1", last_assistant_message=""))

    assert await ledger(octomate) == []


async def test_session_locks_self_clean_and_session_end_finalizes() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    )

    await submit(ingest, "p1", "hello")
    # The lock registry reclaims a key once no task holds it, so a completed turn leaves
    # nothing behind — no manual dismissal, no unbounded growth.
    assert len(ingest.locks.by_session) == 0
    # SessionEnd finalizes the (unstarted here) tailer without error.
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {"hook_event_name": "SessionEnd", "session_id": SESSION_ID, "reason": "x"}
        )
    )
    assert len(ingest.locks.by_session) == 0


async def test_unhandled_events_are_ignored() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager))

    await ingest.handle(
        hook("PreToolUse", "p1", tool_name="Bash", tool_input={"command": "ls"})
    )
    await ingest.handle(hook("MessageDisplay", "p1", delta="thinking...", final=False))

    assert await ledger(octomate) == []
