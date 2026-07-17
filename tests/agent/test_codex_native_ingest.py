from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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


def wired(
    octomate: Octomate, roots: tuple[Path, ...] = ()
) -> tuple[CodexHookIngest, CodexTranscriptTailer]:
    """The ingest only tails rollouts inside a known session tree, so tests that expect
    tailing add the tmp tree they write into — the same injection the tentacle does from
    `agents.codex.transcript_root`, and additive there for the same reason."""
    locks = SessionLocks()
    tailer = CodexTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    return CodexHookIngest(octomate, tailer, locks, extra_transcript_roots=roots), tailer


async def test_hooks_sketch_then_rollout_replaces_with_full_turn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))

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


async def test_a_transcript_outside_the_session_tree_is_not_tailed(
    tmp_path: Path,
) -> None:
    """`transcript_path` is the caller's claim, and following it means reading whatever
    it names into this session's history — so only Codex's own tree is in scope. The
    `..` case is the one a lexical root test would wave through."""
    outside = tmp_path.parent / "outside.jsonl"
    outside.write_text("")
    octomate = Octomate()
    # tmp_path is a known root here, so `outside` beside it is genuinely outside every
    # root — without this the paths would be refused only for being outside the real
    # ~/.codex/sessions, and the test would pass against a gate that never ran.
    ingest, tailer = wired(octomate, (tmp_path,))

    for claimed in (outside, tmp_path / ".." / "outside.jsonl"):
        await ingest.handle(
            CodexHookInput(
                hook_event_name="SessionStart",
                session_id=SESSION_ID,
                transcript_path=claimed,
            )
        )
        assert SESSION_ID not in tailer.sessions


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
    ingest, tailer = wired(octomate, (tmp_path,))

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


def event(second: int, kind: str, **payload: str) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-16T10:00:{second:02d}Z",
        "type": "event_msg",
        "payload": {"type": kind, **payload},
    }


def answer_item(second: int, text: str) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-16T10:00:{second:02d}Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        },
    }


async def pumped_runs(tmp_path: Path, records: list[dict[str, object]]):
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))
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
    await tailer.shutdown()
    return conversation.runs


async def test_an_aborted_turn_closes_and_the_next_turn_still_records(
    tmp_path: Path,
) -> None:
    """The 24% bug: a turn that ends in `turn_aborted` never closed, and the stuck
    open turn then misread every later `task_started` as nested and swallowed the
    rest of the session. One abort must cost at most its own turn's tail."""
    runs = await pumped_runs(
        tmp_path,
        [
            event(0, "task_started", turn_id="turn-a"),
            event(1, "user_message", message="first ask"),
            event(2, "turn_aborted", turn_id="turn-a", reason="interrupted"),
            event(3, "task_started", turn_id="turn-b"),
            event(4, "user_message", message="second ask"),
            answer_item(5, "done twice"),
            event(6, "task_complete", turn_id="turn-b"),
        ],
    )
    assert [run.id for run in runs] == ["turn-a", "turn-b"]
    aborted, completed = runs
    assert [message.message_text for message in aborted.messages] == ["first ask"]
    assert [message.message_text for message in completed.messages] == [
        "second ask",
        "done twice",
    ]


async def test_an_aborted_overlapping_task_pops_instead_of_jamming(
    tmp_path: Path,
) -> None:
    runs = await pumped_runs(
        tmp_path,
        [
            event(0, "task_started", turn_id=TURN_ID),
            event(1, "user_message", message="parent prompt"),
            event(2, "task_started", turn_id="overlap"),
            event(3, "turn_aborted", turn_id="overlap", reason="interrupted"),
            answer_item(4, "parent answer"),
            event(5, "task_complete", turn_id=TURN_ID),
        ],
    )
    [run] = runs
    assert run.id == TURN_ID
    assert [message.message_text for message in run.messages] == [
        "parent prompt",
        "parent answer",
    ]


async def test_the_open_turns_own_end_clears_absorbed_task_starts(
    tmp_path: Path,
) -> None:
    """A `task_started` that never closes (measured: 20 in the corpus) leaves the
    stack non-empty forever; the open turn's own completion must still commit it
    rather than being skipped as someone else's."""
    runs = await pumped_runs(
        tmp_path,
        [
            event(0, "task_started", turn_id=TURN_ID),
            event(1, "user_message", message="parent prompt"),
            event(2, "task_started", turn_id="dangling"),
            event(
                3,
                "task_complete",
                turn_id=TURN_ID,
                last_agent_message="wrapped up",
            ),
        ],
    )
    [run] = runs
    assert run.id == TURN_ID
    assert run.end_offset is not None
    assert [message.message_text for message in run.messages] == ["parent prompt"]


async def test_a_backfilled_row_is_dated_by_the_rollout_not_the_replay(
    tmp_path: Path,
) -> None:
    """A session Octomate only saw after the fact: no hook wrote its ledger, so the
    tailer creates those rows from the rollout, which records when the turn really
    happened. Dating them `now` would be a lie the ledger keeps."""
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))

    # SessionStart alone: the live tier never saw this turn's prompt or answer.
    await ingest.handle(
        CodexHookInput.model_validate(
            {
                "hook_event_name": "SessionStart",
                "session_id": SESSION_ID,
                "transcript_path": path,
            }
        )
    )
    await tailer.pump_session(SESSION_ID)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "private", SESSION_ID, "")
    )
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated  # the tailer did write the ledger

    # The user_message record says 10:00:02, the final assistant message 10:00:05.
    assert dated["inbound"] == datetime(2026, 7, 16, 10, 0, 2, tzinfo=timezone.utc)
    assert dated["outbound"] == datetime(2026, 7, 16, 10, 0, 5, tzinfo=timezone.utc)
    await tailer.shutdown()


async def test_a_live_turn_is_dated_when_it_happened(tmp_path: Path) -> None:
    """A hook carries no clock, and it fires as the turn happens — so receipt time is
    both the best available answer and a true one, to within the round-trip."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    octomate = Octomate()
    ingest, tailer = wired(octomate, (tmp_path,))
    common = {"session_id": SESSION_ID, "turn_id": TURN_ID, "transcript_path": path}
    before = datetime.now(timezone.utc)

    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "UserPromptSubmit", "prompt": "inspect it"}
        )
    )
    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "Stop", "last_assistant_message": "done"}
        )
    )

    after = datetime.now(timezone.utc)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "private", SESSION_ID, "")
    )
    stamps = [message.happened_at for message in thread.messages]
    assert len(stamps) == 2
    assert all(stamp is not None and before <= stamp <= after for stamp in stamps)
    await tailer.shutdown()
