"""UoW-A — live human-ledger ingest of a native Claude Code session's hooks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer
from tests.support.managers import a_loaded_thread

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
    of opening whatever path a hook claims — and the ledger writes either way."""
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = ClaudeHookIngest(octomate, tailer)

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt="hi"), SENDER)

    assert tailer.sessions == {}
    assert await ledger(octomate) == [("inbound", "p1", "hi")]


async def submit(ingest: ClaudeHookIngest, prompt_id: str, prompt: str) -> None:
    await ingest.handle(hook("UserPromptSubmit", prompt_id, prompt=prompt), SENDER)


async def stop(ingest: ClaudeHookIngest, prompt_id: str, answer: str) -> None:
    await ingest.handle(
        hook("Stop", prompt_id, stop_hook_active=False, last_assistant_message=answer),
        SENDER,
    )


async def ledger(octomate: Octomate) -> list[tuple[str, str | None, str | None]]:
    """The thread's chat log as (direction, platform_message_id, text)."""
    thread = await a_loaded_thread(octomate.thread_manager, SESSION_KEY)
    return [
        (m.direction, m.platform_message_id, m.message_text) for m in thread.messages
    ]


async def test_a_turn_writes_inbound_and_outbound_tagged_by_prompt_id() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    await submit(ingest, "p1", "list the files")
    await stop(ingest, "p1", "Here are the files.")

    assert await ledger(octomate) == [
        ("inbound", "p1", "list the files"),
        ("outbound", "p1", "Here are the files."),
    ]


async def test_multiple_turns_accumulate_in_order() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

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
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

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
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    await submit(ingest, "p1", "do a thing")
    # no Stop — the session died mid-turn

    assert await ledger(octomate) == [("inbound", "p1", "do a thing")]


async def test_hooks_sketch_the_turns_run_live() -> None:
    """The hooks alone leave a whole conversation → run → messages chain for the turn,
    so a turn in flight has a model history to hang from before the transcript's real
    timeline lands. The sketch carries no byte range: that is what marks it provisional
    and lets the tailer replace it."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    await submit(ingest, "p1", "hello")

    # The prompt alone already hangs off a run, keyed by the turn's prompt_id.
    assert await sketched(octomate) == [("p1", ["hello"])]

    await stop(ingest, "p1", "hi")

    # Stop carries no prompt, so the answer joins the prompt read back off the ledger.
    assert await sketched(octomate) == [("p1", ["hello", "hi"])]
    async with async_session() as session:
        runs = await session.list(AgentRun, limit=None, order_bys=[])
    assert [(run.start_offset, run.end_offset) for run in runs] == [(None, None)]


async def test_a_session_octomate_drives_is_answered_but_not_recorded() -> None:
    """An operator's hook settings fire for the tentacle's own sessions too, and this
    pipe does not reach around them to stop that. It just declines to record what the
    tentacle is already recording as it drives it — otherwise the same conversation
    would be written twice, once by the runner and once by its own hooks."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    # as the tentacle holds it: from before the session is launched until its client's
    # teardown has waited the CLI out.
    with ingest.driving(SESSION_ID):
        await submit(ingest, "p1", "hello")
        await stop(ingest, "p1", "hi")
        await ingest.handle(hook("SessionEnd", reason="other"), SENDER)

    assert await ledger(octomate) == []  # no chat log
    assert await sketched(octomate) == []  # no run
    async with async_session() as session:
        assert await session.list(AgentRun, limit=None, order_bys=[]) == []


async def test_a_session_octomate_does_not_drive_is_still_recorded() -> None:
    """Claiming one session says nothing about the next: a native client's session runs
    alongside the tentacle's and is ingested as usual."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    with ingest.driving("some-other-session"):
        await submit(ingest, "p1", "hello")

    assert await ledger(octomate) == [("inbound", "p1", "hello")]


async def test_the_claim_outlives_the_first_of_two_overlapping_runs() -> None:
    """A follow-up run supersedes a live one on the same session, and the two overlap
    while the first unwinds. The claim is counted, so the run that ends first does not
    strip it from the one still driving."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    with ingest.driving(SESSION_ID):  # the superseded run
        with ingest.driving(
            SESSION_ID
        ):  # the follow-up, taken before the first unwinds
            pass
        await submit(ingest, "p1", "hello")  # still driven, so still not ingested

    assert await ledger(octomate) == []
    assert ingest.driven == {}  # both released: nothing kept once no run holds it

    await submit(ingest, "p2", "after")  # the claim is gone, so this is a native turn
    assert await ledger(octomate) == [("inbound", "p2", "after")]


async def test_a_sketch_is_dated_so_it_sorts_after_the_history() -> None:
    """`Conversation.runs` and `.messages` both order on `started_at`, which is read off
    the run's first message — and `ModelRequest.timestamp` defaults to None. An undated
    sketch would sort ahead of every turn before it, putting the live prompt at the head
    of the history it belongs at the end of."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

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
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt=""), SENDER)
    await ingest.handle(hook("Stop", "p1", last_assistant_message=""), SENDER)

    assert await ledger(octomate) == []


async def test_session_locks_self_clean_and_session_end_finalizes() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
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
        SENDER,
    )
    assert len(ingest.locks.by_session) == 0


async def test_unhandled_events_are_ignored() -> None:
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )

    await ingest.handle(
        hook("PreToolUse", "p1", tool_name="Bash", tool_input={"command": "ls"}), SENDER
    )
    await ingest.handle(
        hook("MessageDisplay", "p1", delta="thinking...", final=False), SENDER
    )

    assert await ledger(octomate) == []


async def test_a_live_turn_is_dated_when_it_happened() -> None:
    """A hook carries no clock, and it fires as the turn happens — so receipt time is
    both the best available answer and a true one, to within the round-trip. An undated
    row is the thing to avoid: `created_at` alone cannot say whether a row is a live
    turn or history the tailer replayed."""
    octomate = Octomate()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )
    before = datetime.now(UTC)

    await submit(ingest, "p1", "list the files")
    await stop(ingest, "p1", "Here are the files.")

    after = datetime.now(UTC)
    thread = await a_loaded_thread(octomate.thread_manager, SESSION_KEY)
    stamps = [message.happened_at for message in thread.messages]
    assert len(stamps) == 2
    assert all(stamp is not None and before <= stamp <= after for stamp in stamps)


async def test_the_ledger_row_belongs_to_the_bearers_user() -> None:
    """The principal is the point: an ingested prompt's sender profile is owned
    by the user whose token authenticated the hook, so two humans' terminals
    write distinguishable history."""
    octomate = Octomate(
        users=UserManager({"lu": UserConfig.model_validate({"secret": "lu-token"})})
    )
    await octomate.users.reconcile()
    ingest = ClaudeHookIngest(
        octomate,
        ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager),
    )
    bearer = await octomate.users.native_profile(CLAUDE_NATIVE_ID, "lu")
    assert bearer is not None

    await ingest.handle(hook("UserPromptSubmit", "p1", prompt="hi"), bearer)

    thread = await a_loaded_thread(octomate.thread_manager, SESSION_KEY)
    [row] = thread.messages
    assert row.user_id == "lu"
    profile = await octomate.users.profile(CLAUDE_NATIVE_ID, "lu")
    assert profile is not None
    assert row.sender_id == profile.id
    owner = await octomate.users.owner(profile)
    assert owner is not None
    assert owner.username == "lu"
