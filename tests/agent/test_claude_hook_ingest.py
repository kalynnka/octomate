"""Hooks reserve run identities; only streamed transcripts create messages."""

from __future__ import annotations

import pytest
from pydantic import JsonValue
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer
from tests.support.managers import a_loaded_thread, a_thread

SENDER = UserProfile(channel_user_id="lu", name="lu")

SESSION_ID = "sess-1"
SESSION_KEY = ThreadKey(CLAUDE_NATIVE_ID, "thread", SESSION_ID)


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


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


async def test_a_hooks_transcript_path_is_never_followed() -> None:
    """`transcript_path` is recorded context, never something to follow: the stream
    is the only assembler, so the hook pipe must not put the server in the business
    of opening whatever path a hook claims."""
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.threads)
    ingest = ClaudeHookIngest(
        tailer,
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt="hi"))

    assert tailer.sessions == {}
    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", [])]


async def submit(ingest: ClaudeHookIngest, prompt_id: str, prompt: str) -> None:
    await ingest.handle(hook("UserPromptSubmit", prompt_id, prompt=prompt))


async def stop(ingest: ClaudeHookIngest, prompt_id: str, answer: str) -> None:
    await ingest.handle(
        hook("Stop", prompt_id, stop_hook_active=False, last_assistant_message=answer),
    )


async def ledger(octomate: Octomate) -> list[tuple[str, str | None, str | None]]:
    """The thread's chat log as (direction, platform_message_id, text)."""
    thread = await a_loaded_thread(octomate.threads, SESSION_KEY)
    return [
        (m.direction, m.platform_message_id, m.message_text) for m in thread.messages
    ]


async def test_hooks_reserve_a_run_without_writing_messages() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "list the files")
    await stop(ingest, "p1", "Here are the files.")

    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", [])]


async def test_multiple_turns_reserve_independent_runs() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "first")
    await stop(ingest, "p1", "first done")
    await submit(ingest, "p2", "second")
    await stop(ingest, "p2", "second done")

    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", []), ("p2", [])]


async def test_refiring_events_is_idempotent() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "list the files")
    await submit(ingest, "p1", "list the files")  # retry
    await stop(ingest, "p1", "done")
    await stop(ingest, "p1", "done")  # a repeated Stop

    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", [])]


async def test_missing_stop_leaves_an_empty_run() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "do a thing")
    # no Stop — the session died mid-turn

    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", [])]


async def test_repeated_hooks_leave_the_same_empty_run() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "hello")

    assert await reserved(octomate) == [("p1", [])]
    await stop(ingest, "p1", "hi")
    await submit(ingest, "p1", "a repeated prompt")
    assert await reserved(octomate) == [("p1", [])]
    assert await ledger(octomate) == []
    async with async_session() as session:
        [run] = await session.list(AgentRun, limit=None, order_bys=[])
    assert run.end_offset is None


async def test_hooks_for_an_sdk_session_are_recorded_as_external() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )
    sdk_conversation = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id="claude"
    )
    await octomate.conversations.record_agent_run(
        sdk_conversation,
        run_id="sdk-run",
        messages=[ModelRequest(parts=[UserPromptPart(content="SDK prompt")])],
        external_id=SESSION_ID,
    )

    await submit(ingest, "p1", "hello")
    await stop(ingest, "p1", "hi")
    await ingest.handle(hook("SessionEnd", reason="other"))

    assert await ledger(octomate) == []
    assert await reserved(octomate) == [("p1", [])]
    sdk = await octomate.conversations.get(sdk_conversation.id)
    assert sdk is not None
    assert sdk.external_id == SESSION_ID
    assert [run.id for run in sdk.runs] == ["sdk-run"]
    assert [message.message_text for message in sdk.messages] == ["SDK prompt"]


async def reserved(octomate: Octomate) -> list[tuple[str, list[str | None]]]:
    """Each run of the session's conversation as (run_id, its messages' text)."""
    thread = await octomate.threads.ensure(SESSION_KEY)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    return [
        (run.id, [message.message_text for message in run.messages])
        for run in conversation.runs
    ]


async def test_empty_prompt_and_empty_answer_are_skipped() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt=""))
    await ingest.handle(hook("Stop", "p1", last_assistant_message=""))

    assert await ledger(octomate) == []


async def test_session_locks_self_clean_and_session_end_finalizes() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await submit(ingest, "p1", "hello")
    # The lock registry reclaims a key once no task holds it, so a completed turn leaves
    # nothing behind — no manual dismissal, no unbounded growth.
    assert len(ingest.locks.by_session) == 0
    # SessionEnd finalizes the (unstarted here) tailer without error.
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {"hook_event_name": "SessionEnd", "session_id": SESSION_ID, "reason": "x"}
        ),
    )
    assert len(ingest.locks.by_session) == 0


async def test_unhandled_events_are_ignored() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        ClaudeTranscriptTailer(octomate.conversations, octomate.threads),
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    )

    await ingest.handle(
        hook("PreToolUse", "p1", tool_name="Bash", tool_input={"command": "ls"})
    )
    await ingest.handle(hook("MessageDisplay", "p1", delta="thinking...", final=False))

    assert await ledger(octomate) == []
