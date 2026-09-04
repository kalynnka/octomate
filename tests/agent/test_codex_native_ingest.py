from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.codex.tailer import CodexTranscriptTailer, TailState
from octomate.tentacles.codex.transcript import rollout_line_adapter
from octomate.tentacles.locks import SessionLocks

SENDER = UserProfile(channel_user_id="lu", name="lu")

SESSION_ID = "codex-session"
TURN_ID = "codex-turn"
ROOT_THREAD_ID = "019f6a89-cb1e-7730-be3b-ed450f8cbb0e"
PARENT_TURN_ID = "019f6b27-d8e1-7eb2-bf81-aeefbaabbed0"
CHILD_THREAD_ID = "019f6b28-3ad5-78b0-979f-70a0f1661b0b"
CHILD_TURN_ONE = "019f6b28-5ff5-7b01-b25c-44b7126d508f"
CHILD_TURN_TWO = "019f6b2d-b541-7090-8ea7-95bbe0b68c27"


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


# The rollout in the *client's* namespace: a label the server records, never opens.
ROLLOUT_LABEL = Path("/laptop/.codex/sessions/rollout.jsonl")


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


async def feed_records(
    tailer: CodexTranscriptTailer,
    state: TailState,
    records: list[dict[str, object]],
    *,
    key: str | None = None,
    start: int = 0,
) -> int:
    """Feed records as the stream client frames them — complete lines with the byte
    range each occupies in its own file — returning the offset past the last one."""
    offset = start
    for record in records:
        raw = json.dumps(record)
        end = offset + len(raw.encode()) + 1
        await tailer.feed_remote(state, key, raw, offset, end)
        offset = end
    return offset


async def stream_rollout(
    tailer: CodexTranscriptTailer,
    session_id: str,
    rollout: Path,
) -> None:
    """Stream one rollout file the way production reaches it: attach, feed its framed
    lines, detach — the server never opens the file itself."""
    state, _ = await tailer.attach_remote(session_id, rollout, SENDER)
    offset = 0
    for raw in rollout.read_bytes().split(b"\n")[:-1]:
        end = offset + len(raw) + 1
        await tailer.feed_remote(state, None, raw.decode(), offset, end)
        offset = end
    tailer.detach_remote(state)


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
        ),
        SENDER,
    )
    await ingest.handle(
        CodexHookInput.model_validate(
            {
                **common,
                "hook_event_name": "Stop",
                "last_assistant_message": "done",
            }
        ),
        SENDER,
    )

    conversation = await octomate.conversations.ensure(
        (
            await octomate.thread_manager.ensure(
                ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
            )
        ).id,
        agent_tentacle_id=CODEX_NATIVE_ID,
    )
    [sketch] = conversation.runs
    assert isinstance(sketch, ExternalAgentRun)
    assert sketch.end_offset is None

    write_rollout(path)
    await stream_rollout(tailer, SESSION_ID, path)

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


async def test_a_session_start_hook_never_tails_the_claimed_path(
    tmp_path: Path,
) -> None:
    """`transcript_path` is recorded context, never something to follow: the stream
    is the only assembler, so the hook pipe must not put the server in the business
    of opening whatever path a hook claims."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        CodexHookInput(
            hook_event_name="SessionStart",
            session_id=SESSION_ID,
            transcript_path=tmp_path / "rollout.jsonl",
        ),
        SENDER,
    )
    assert tailer.sessions == {}


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
            ),
            SENDER,
        )

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
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
        ),
        SENDER,
    )

    assert await octomate.thread_manager.list_threads() == []
    await tailer.shutdown()


def event(second: int, kind: str, **payload: str) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-16T10:00:{second:02d}Z",
        "type": "event_msg",
        "payload": {"type": kind, **payload},
    }


def answer_item(
    second: int, text: str, *, phase: str = "final_answer"
) -> dict[str, object]:
    return {
        "timestamp": f"2026-07-16T10:00:{second:02d}Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def parent_metadata() -> dict[str, object]:
    return {
        "timestamp": "2026-07-16T10:00:00Z",
        "type": "session_meta",
        "payload": {
            "session_id": SESSION_ID,
            "id": ROOT_THREAD_ID,
            "originator": "codex_cli",
            "source": "cli",
            "thread_source": "user",
        },
    }


def child_metadata(
    *, child_thread_id: str = CHILD_THREAD_ID, source: str = "thread_spawn"
) -> dict[str, object]:
    subagent = (
        {
            "thread_spawn": {
                "parent_thread_id": ROOT_THREAD_ID,
                "depth": 1,
                "agent_path": "/root/audit",
            }
        }
        if source == "thread_spawn"
        else {"other": source}
    )
    return {
        "timestamp": "2026-07-16T10:00:02Z",
        "type": "session_meta",
        "payload": {
            "session_id": SESSION_ID,
            "id": child_thread_id,
            "originator": "codex_cli",
            "source": {"subagent": subagent},
            "thread_source": "subagent",
        },
    }


def subagent_activity(
    second: int, tool_call_id: str, *, kind: str
) -> dict[str, object]:
    happened_at = datetime(2026, 7, 16, 10, 0, second, tzinfo=UTC)
    return {
        "timestamp": happened_at.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "sub_agent_activity",
            "event_id": tool_call_id,
            "occurred_at_ms": int(happened_at.timestamp() * 1000),
            "agent_thread_id": CHILD_THREAD_ID,
            "agent_path": "/root/audit",
            "kind": kind,
        },
    }


async def streamed_runs(records: list[dict[str, object]]):
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, ROLLOUT_LABEL, SENDER)
    await feed_records(tailer, state, records)
    tailer.detach_remote(state)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    return conversation.runs


async def test_an_aborted_turn_closes_and_the_next_turn_still_records() -> None:
    """The 24% bug: a turn that ends in `turn_aborted` never closed, and the stuck
    open turn then misread every later `task_started` as nested and swallowed the
    rest of the session. One abort must cost at most its own turn's tail."""
    runs = await streamed_runs(
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


async def test_a_second_task_started_interrupts_and_replaces_the_open_turn() -> None:
    runs = await streamed_runs(
        [
            event(0, "task_started", turn_id=TURN_ID),
            event(1, "user_message", message="parent prompt"),
            event(2, "task_started", turn_id="replacement"),
            event(3, "user_message", message="replacement prompt"),
            answer_item(4, "replacement answer"),
            event(5, "task_complete", turn_id="replacement"),
        ],
    )
    assert [run.id for run in runs] == [TURN_ID, "replacement"]
    interrupted, replacement = runs
    assert [message.message_text for message in interrupted.messages] == [
        "parent prompt",
    ]
    assert [message.message_text for message in replacement.messages] == [
        "replacement prompt",
        "replacement answer",
    ]


async def test_a_backfilled_row_is_dated_by_the_rollout_not_the_replay(
    tmp_path: Path,
) -> None:
    """A session Octomate only saw after the fact: no hook wrote its ledger, so the
    tailer creates those rows from the rollout, which records when the turn really
    happened. Dating them `now` would be a lie the ledger keeps."""
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    octomate = Octomate()
    _, tailer = wired(octomate)

    # The stream alone: the live tier never saw this turn's prompt or answer.
    await stream_rollout(tailer, SESSION_ID, path)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated  # the tailer did write the ledger

    # The user_message record says 10:00:02, the final assistant message 10:00:05.
    assert dated["inbound"] == datetime(2026, 7, 16, 10, 0, 2, tzinfo=UTC)
    assert dated["outbound"] == datetime(2026, 7, 16, 10, 0, 5, tzinfo=UTC)
    await tailer.shutdown()


async def test_a_live_turn_is_dated_when_it_happened(tmp_path: Path) -> None:
    """A hook carries no clock, and it fires as the turn happens — so receipt time is
    both the best available answer and a true one, to within the round-trip."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    common = {"session_id": SESSION_ID, "turn_id": TURN_ID, "transcript_path": path}
    before = datetime.now(UTC)

    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "UserPromptSubmit", "prompt": "inspect it"}
        ),
        SENDER,
    )
    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "Stop", "last_assistant_message": "done"}
        ),
        SENDER,
    )

    after = datetime.now(UTC)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    stamps = [message.happened_at for message in thread.messages]
    assert len(stamps) == 2
    assert all(stamp is not None and before <= stamp <= after for stamp in stamps)
    await tailer.shutdown()


async def test_thread_spawn_rollout_tree_records_resumed_child_runs() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, ROLLOUT_LABEL, SENDER)

    await feed_records(
        tailer,
        state,
        [
            parent_metadata(),
            event(0, "task_started", turn_id=PARENT_TURN_ID),
            event(1, "user_message", message="delegate the audit"),
            subagent_activity(2, "call-spawn", kind="started"),
            subagent_activity(6, "call-followup", kind="interacted"),
            answer_item(10, "parent done"),
            event(11, "task_complete", turn_id=PARENT_TURN_ID),
        ],
    )
    await feed_records(
        tailer,
        state,
        [
            child_metadata(),
            parent_metadata(),
            event(0, "task_started", turn_id=PARENT_TURN_ID),
            event(1, "user_message", message="fork replay"),
            event(2, "task_complete", turn_id=PARENT_TURN_ID),
            event(3, "task_started", turn_id=CHILD_TURN_ONE),
            answer_item(4, "first finding"),
            event(5, "task_complete", turn_id=CHILD_TURN_ONE),
            event(7, "task_started", turn_id=CHILD_TURN_TWO),
            answer_item(8, "second finding"),
            event(9, "task_complete", turn_id=CHILD_TURN_TWO),
        ],
        key=f"rollout-child-{CHILD_THREAD_ID}.jsonl",
    )
    tailer.detach_remote(state)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    parent = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id=CODEX_NATIVE_ID,
        subagent_id=CHILD_THREAD_ID,
        parent_conversation_id=parent.id,
    )

    assert child.parent_conversation_id == parent.id
    assert child.external_id == CHILD_THREAD_ID
    assert [run.id for run in child.runs] == [CHILD_TURN_ONE, CHILD_TURN_TWO]
    assert [run.parent_run_id for run in child.runs] == [
        PARENT_TURN_ID,
        PARENT_TURN_ID,
    ]
    assert [run.parent_tool_call_id for run in child.runs] == [
        "call-spawn",
        "call-followup",
    ]
    assert [message.message_text for message in child.messages] == [
        "first finding",
        "second finding",
    ]
    assert [run.id for run in parent.runs] == [PARENT_TURN_ID]
    assert [message.message_text for message in thread.messages] == [
        "delegate the audit",
        "parent done",
    ]
    await tailer.shutdown()


async def test_child_turn_links_activity_that_arrives_after_it_starts() -> None:
    child_key = f"rollout-child-{CHILD_THREAD_ID}.jsonl"
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, ROLLOUT_LABEL, SENDER)

    parent_offset = await feed_records(
        tailer,
        state,
        [
            parent_metadata(),
            event(0, "task_started", turn_id=PARENT_TURN_ID),
        ],
    )
    child_offset = await feed_records(
        tailer,
        state,
        [
            child_metadata(),
            event(3, "task_started", turn_id=CHILD_TURN_ONE),
            answer_item(4, "working", phase="commentary"),
        ],
        key=child_key,
    )
    await feed_records(
        tailer,
        state,
        [subagent_activity(2, "call-after-start", kind="started")],
        start=parent_offset,
    )
    await feed_records(
        tailer,
        state,
        [
            answer_item(5, "done"),
            event(6, "task_complete", turn_id=CHILD_TURN_ONE),
        ],
        key=child_key,
        start=child_offset,
    )
    tailer.detach_remote(state)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    parent = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id=CODEX_NATIVE_ID,
        subagent_id=CHILD_THREAD_ID,
        parent_conversation_id=parent.id,
    )
    [run] = child.runs
    assert run.parent_run_id == PARENT_TURN_ID
    assert run.parent_tool_call_id == "call-after-start"
    await tailer.shutdown()


async def test_guardian_rollout_is_not_ingested_as_a_subagent() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, ROLLOUT_LABEL, SENDER)

    await feed_records(tailer, state, [parent_metadata()])
    await feed_records(
        tailer,
        state,
        [
            child_metadata(
                child_thread_id="019f6b28-4ad5-78b0-979f-70a0f1661b0b",
                source="guardian",
            ),
            event(
                3,
                "task_started",
                turn_id="019f6b28-6ff5-7b01-b25c-44b7126d508f",
            ),
            answer_item(4, "internal verdict"),
        ],
        key="rollout-guardian.jsonl",
    )
    tailer.detach_remote(state)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    assert all(
        conversation.subagent_id == ""
        for conversation in await octomate.conversations.for_thread(thread.id)
    )
    await tailer.shutdown()


def test_known_declined_rollout_line_types_are_modeled() -> None:
    for line_type in ("compacted", "inter_agent_communication_metadata"):
        line = rollout_line_adapter.validate_python(
            {
                "timestamp": "2026-07-16T10:00:00Z",
                "type": line_type,
                "payload": {},
            }
        )
        assert line.type == line_type
