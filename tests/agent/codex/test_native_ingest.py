from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from anyio.abc import ObjectSendStream
from arcanus.materia.sqlalchemy import selectinload
from pydantic_ai.messages import (
    CompactionPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.nerve import Impulse, Nerve
from octomate.schemas.compactions import Compaction, ExternalCompaction
from octomate.schemas.messages import ModelMessage
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.codex.ingest import CodexHookIngest
from octomate.tentacles.codex.tailer import CodexTranscriptTailer, TailState
from octomate.tentacles.codex.transcript import CompactedItem, rollout_line_adapter
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject
from tests.support.managers import a_loaded_thread, a_thread

SENDER = UserProfile(channel_user_id="lu", name="lu")

SESSION_ID = "codex-session"
SESSION_ADDRESS = ThreadKey("codex", "thread", SESSION_ID).address(
    SENDER.channel_user_id
)


@pytest.mark.parametrize("order", ["driven_first", "native_first", "interleaved"])
@pytest.mark.parametrize("driven_content", [False, True])
@pytest.mark.parametrize("native_content", [False, True])
async def test_native_completion_correlates_with_driven_compaction_without_new_identities(
    in_memory_engine: AsyncEngine,
    order: str,
    driven_content: bool,
    native_content: bool,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    owner = await app.conversations.ensure(await a_thread(), agent_tentacle_id="codex")
    await app.conversations.set_external_id(owner, SESSION_ID)
    payload: JsonObject = {"message": "Compacted earlier work"}
    records = [
        event(0, "task_started", turn_id=TURN_ID),
        {
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "compacted",
            "payload": payload if native_content else {"message": ""},
        },
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": SESSION_ID,
                "turn_id": TURN_ID,
                "item": {"type": "ContextCompaction", "id": "compaction-item"},
            },
        },
        event(3, "task_complete", turn_id=TURN_ID),
    ]

    async def driven() -> None:
        messages = CompactedItem.model_validate(payload).to_messages(
            datetime(2026, 7, 16, 10, 0, 1, tzinfo=UTC)
        )
        await app.conversations.record_driven_compaction(
            owner,
            native_session_id=SESSION_ID,
            native_compaction_id="compaction-item",
            trigger="manual",
            happened_at=None,
            messages=messages if driven_content else None,
        )

    async def identities() -> tuple[str, list[str]]:
        async with async_session() as session:
            rows = await session.list(
                Compaction,
                order_bys=[Compaction["recorded_at"]],
                options=[selectinload(Compaction["messages"])],
            )
        first = rows[0]
        return str(first.id), [str(message.id) for message in first.messages]

    async with app.nerve.publish(SESSION_ADDRESS) as impulses:
        state, _ = await tailer.attach_remote(
            SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
        )
        if order == "driven_first":
            await driven()
            original_id, original_messages = await identities()
            await feed_records(tailer, state, records)
        else:
            offset = await feed_records(tailer, state, records[:2])
            original_id, original_messages = await identities()
            if order == "interleaved":
                await driven()
            await feed_records(tailer, state, records[2:], start=offset)
            if order == "native_first":
                await driven()
        tailer.detach_remote(state)
        state, _ = await tailer.attach_remote(
            SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
        )
        await feed_records(tailer, state, records)
        tailer.detach_remote(state)
    async with async_session() as session:
        [stored] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
        assert await session.count(ExternalAgentRun) == 0
        assert await session.count(ModelMessage) == int(
            native_content or driven_content
        )
    assert str(stored.id) == original_id
    assert stored.native_compaction_id == "compaction-item"
    assert stored.conversation_id == owner.id
    assert stored.trigger == "manual"
    if native_content or driven_content:
        assert stored.messages[0].message_text == "Compacted earlier work"
    if original_messages:
        assert [str(message.id) for message in stored.messages] == original_messages


@pytest.fixture
async def impulses() -> AsyncGenerator[ObjectSendStream[Impulse]]:
    async with Nerve().publish(SESSION_ADDRESS) as publisher:
        yield publisher


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
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "turn_context",
            "payload": {
                "model": "gpt-6-luna",
                "approval_policy": "on-request",
                "approvals_reviewer": "auto_review",
                "sandbox_policy": {"type": "workspace-write"},
            },
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
        octomate.conversations,
        octomate.threads,
        octomate.files,
        locks,
        agent_tentacle_id="codex",
    )
    return CodexHookIngest(
        tailer,
        locks,
        conversations=octomate.conversations,
        projects=octomate.projects,
        threads=octomate.threads,
    ), tailer


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
    """Stream one client file through the same scoped publisher as the route."""
    address = ThreadKey("codex", "thread", session_id).address(SENDER.channel_user_id)
    async with Nerve().publish(address) as publisher:
        state, _ = await tailer.attach_remote(
            session_id, rollout, SENDER, address, publisher
        )
        offset = 0
        for raw in (await anyio.Path(rollout).read_bytes()).split(b"\n")[:-1]:
            end = offset + len(raw) + 1
            await tailer.feed_remote(state, None, raw.decode(), offset, end)
            offset = end
        tailer.detach_remote(state)


async def test_context_snapshot_and_last_response_usage_persist_together(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records.insert(
        -1,
        {
            "timestamp": "2026-07-16T10:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "model_context_window": 262144,
                    "last_token_usage": {
                        "input_tokens": 42,
                        "output_tokens": 3,
                        "cached_input_tokens": 7,
                    },
                },
            },
        },
    )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    octomate = Octomate()
    _, tailer = wired(octomate)
    await stream_rollout(tailer, SESSION_ID, path)
    thread = await octomate.threads.ensure(
        ThreadKey("codex", "thread", SESSION_ID), native=True
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    [run] = conversation.runs
    assert run.context_used == 42
    assert run.context_window == 262144
    last = run.messages[-1]
    assert isinstance(last, ModelResponse)
    assert last.usage.input_tokens == run.context_used
    assert last.usage.cache_read_tokens == 7


async def test_hooks_bind_session_and_only_rollout_writes_runs_and_messages(
    tmp_path: Path,
    impulses: ObjectSendStream[Impulse],
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
    )
    thread = await octomate.threads.ensure(
        ThreadKey("codex", "thread", SESSION_ID), native=True
    )
    unbound = await octomate.threads.messages_page(thread.id, unbound=True, limit=100)
    assert not unbound.items
    assert not (await octomate.conversations.messages_page(thread.id, limit=100)).items
    await ingest.handle(
        CodexHookInput.model_validate(
            {
                **common,
                "hook_event_name": "Stop",
                "last_assistant_message": "done",
            }
        ),
    )

    conversation = await octomate.conversations.ensure(
        (
            await octomate.threads.ensure(
                ThreadKey("codex", "thread", SESSION_ID), native=True
            )
        ).id,
        agent_tentacle_id="codex",
    )
    assert conversation.external_id == SESSION_ID
    assert not conversation.runs
    async with async_session() as session:
        assert await session.count(ExternalAgentRun) == 0
    unbound = await octomate.threads.messages_page(thread.id, unbound=True, limit=100)
    assert not unbound.items
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    assert not page.items

    write_rollout(path)
    await stream_rollout(tailer, SESSION_ID, path)

    conversation = await octomate.conversations.ensure(
        conversation.thread_id, agent_tentacle_id="codex"
    )
    [run] = conversation.runs
    assert isinstance(run, ExternalAgentRun)
    assert run.id == TURN_ID
    assert run.end_offset == path.stat().st_size
    assert run.model_name == "gpt-6-luna"
    assert run.permission_mode == "auto_review"
    assert [message.message_text for message in run.messages] == [
        "inspect it",
        None,
        "done",
    ]
    unbound = await octomate.threads.messages_page(thread.id, unbound=True, limit=100)
    assert not unbound.items
    page = await octomate.conversations.messages_page(thread.id, limit=100)
    assert [
        row.platform_message_id
        for message in page.items
        for row in message.thread_messages
        if row.direction == "inbound"
    ] == [TURN_ID]
    await tailer.shutdown()


@pytest.mark.parametrize(
    ("sandbox", "supported"),
    [
        ({"type": "workspace-write"}, True),
        ({"type": "read-only"}, False),
        ({"type": "workspace-write", "network_access": True}, False),
    ],
)
async def test_turn_settings_are_captured_at_context_and_persisted_at_completion(
    sandbox: JsonObject,
    supported: bool,
    impulses: ObjectSendStream[Impulse],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="octomate.tentacles.codex.tailer")
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    offset = await feed_records(
        tailer,
        state,
        [
            event(1, "task_started", turn_id=TURN_ID),
            {
                "timestamp": "2026-07-16T10:00:02Z",
                "type": "turn_context",
                "payload": {
                    "model": "gpt-6-luna",
                    "approval_policy": "on-request",
                    "approvals_reviewer": "auto_review",
                    "sandbox_policy": sandbox,
                },
            },
        ],
    )
    assert state.open_turn is not None
    assert state.open_turn.model_name == "gpt-6-luna"
    assert state.open_turn.permission_mode == ("auto_review" if supported else None)
    if not supported:
        assert "history sync continues, but forking is unavailable" in caplog.text
        assert "sandbox_policy" in caplog.text
    if sandbox.get("network_access"):
        assert "network_access: Input should be False" in caplog.text
    assert all(
        record.levelno < logging.WARNING
        for record in caplog.records
        if record.name == "octomate.tentacles.codex.tailer"
    )
    assert state.conversation is not None
    assert not (await octomate.conversations.get(state.conversation.id)).runs
    await feed_records(
        tailer,
        state,
        [answer_item(3, "done"), event(4, "task_complete", turn_id=TURN_ID)],
        start=offset,
    )
    completed = await octomate.conversations.get(state.conversation.id)
    assert completed.runs[-1].model_name == "gpt-6-luna"
    assert completed.runs[-1].permission_mode == ("auto_review" if supported else None)
    assert [message.message_text for message in completed.runs[-1].messages] == ["done"]
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
    )
    assert tailer.sessions == {}


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
async def test_hooks_persist_and_revise_the_codex_session_name(event: str) -> None:
    octomate = Octomate()
    ingest, _ = wired(octomate)
    key = ThreadKey("codex", "thread", SESSION_ID)
    await ingest.handle(
        CodexHookInput(
            hook_event_name="UserPromptSubmit",
            session_id=SESSION_ID,
            prompt="the opening line",
        ),
    )
    thread = await a_loaded_thread(octomate.threads, key)
    assert thread.title is None

    for name in [" First generated name ", "Revised name", "Revised name", None, " "]:
        await ingest.handle(
            CodexHookInput(
                hook_event_name=event, session_id=SESSION_ID, session_name=name
            ),
        )
        thread = await a_loaded_thread(octomate.threads, key)
        conversation = await octomate.conversations.ensure(
            thread.id, agent_tentacle_id="codex"
        )
        expected = (
            "First generated name"
            if name == " First generated name "
            else "Revised name"
        )
        assert conversation.name == expected
        assert thread.title == expected


async def test_a_child_hook_cannot_rename_the_parent_session() -> None:
    octomate = Octomate()
    ingest, _ = wired(octomate)
    await ingest.handle(
        CodexHookInput(
            hook_event_name="SessionStart",
            session_id=SESSION_ID,
            session_name="Parent name",
        ),
    )
    await ingest.handle(
        CodexHookInput(
            hook_event_name="Stop",
            session_id=SESSION_ID,
            agent_id=CHILD_THREAD_ID,
            session_name="Child name",
        ),
    )
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    assert conversation.name == "Parent name"
    assert thread.title == "Parent name"


async def test_hooks_for_an_sdk_session_are_recorded_as_external() -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    sdk_conversation = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id="codex"
    )
    await octomate.conversations.record_agent_run(
        sdk_conversation,
        run_id="sdk-run",
        messages=[ModelRequest(parts=[UserPromptPart(content="SDK prompt")])],
        external_id=SESSION_ID,
    )

    await ingest.handle(
        CodexHookInput(
            hook_event_name="UserPromptSubmit",
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            prompt="ingest this",
        ),
    )

    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    assert not thread.messages
    sdk = await octomate.conversations.get(sdk_conversation.id)
    assert sdk is not None
    assert sdk.external_id == SESSION_ID
    assert [run.id for run in sdk.runs] == ["sdk-run"]
    assert [
        message.message_text
        for message in (await ConversationManager().history(sdk.id))
    ] == ["SDK prompt"]
    await tailer.shutdown()


async def test_a_legacy_driven_flag_does_not_suppress_ingestion() -> None:
    octomate = Octomate()
    ingest, tailer = wired(octomate)

    await ingest.handle(
        CodexHookInput.model_validate(
            {
                "hook_event_name": "SessionStart",
                "session_id": SESSION_ID,
                "octomate_driven": True,
            }
        ),
    )

    [thread] = await octomate.threads.list_threads()
    assert thread.key == ThreadKey("codex", "thread", SESSION_ID)
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


async def streamed_runs(
    impulses: ObjectSendStream[Impulse], records: list[dict[str, object]]
):
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    await feed_records(tailer, state, records)
    tailer.detach_remote(state)
    thread = await octomate.threads.ensure(
        ThreadKey("codex", "thread", SESSION_ID), native=True
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    return conversation.runs


async def test_an_aborted_turn_closes_and_the_next_turn_still_records(
    impulses: ObjectSendStream[Impulse],
) -> None:
    """The 24% bug: a turn that ends in `turn_aborted` never closed, and the stuck
    open turn then misread every later `task_started` as nested and swallowed the
    rest of the session. One abort must cost at most its own turn's tail."""
    runs = await streamed_runs(
        impulses,
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


async def test_a_second_task_started_interrupts_and_replaces_the_open_turn(
    impulses: ObjectSendStream[Impulse],
) -> None:
    runs = await streamed_runs(
        impulses,
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
    impulses: ObjectSendStream[Impulse],
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

    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    dated = {message.direction: message.happened_at for message in thread.messages}
    assert dated  # the tailer did write the ledger

    # The user_message record says 10:00:02, the final assistant message 10:00:05.
    assert dated["inbound"] == datetime(2026, 7, 16, 10, 0, 2, tzinfo=UTC)
    assert dated["outbound"] == datetime(2026, 7, 16, 10, 0, 5, tzinfo=UTC)
    await tailer.shutdown()


async def test_hook_receipt_time_never_becomes_message_time(
    tmp_path: Path,
    impulses: ObjectSendStream[Impulse],
) -> None:
    """Even a delayed import takes both message clocks from the rollout."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    common = {"session_id": SESSION_ID, "turn_id": TURN_ID, "transcript_path": path}

    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "UserPromptSubmit", "prompt": "inspect it"}
        ),
    )
    await ingest.handle(
        CodexHookInput.model_validate(
            {**common, "hook_event_name": "Stop", "last_assistant_message": "done"}
        ),
    )

    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    assert not thread.messages
    write_rollout(path)
    await stream_rollout(tailer, SESSION_ID, path)
    thread = await a_loaded_thread(octomate.threads, thread.key)
    assert [message.happened_at for message in thread.messages] == [
        datetime(2026, 7, 16, 10, 0, 2, tzinfo=UTC),
        datetime(2026, 7, 16, 10, 0, 5, tzinfo=UTC),
    ]
    await tailer.shutdown()


async def test_thread_spawn_rollout_tree_records_resumed_child_runs(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )

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

    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    parent = await octomate.conversations.ensure(thread.id, agent_tentacle_id="codex")
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id="codex",
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
    assert [
        message.message_text
        for message in (await ConversationManager().history(child.id))
    ] == [
        "first finding",
        "second finding",
    ]
    assert [run.id for run in parent.runs] == [PARENT_TURN_ID]
    assert [message.message_text for message in thread.messages] == [
        "delegate the audit",
        "parent done",
    ]
    await tailer.shutdown()


async def test_guardian_rollout_is_not_ingested_as_a_subagent(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )

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

    thread = await octomate.threads.ensure(
        ThreadKey("codex", "thread", SESSION_ID), native=True
    )
    assert all(
        conversation.subagent_id == ""
        for conversation in await octomate.conversations.for_thread(thread.id)
    )
    await tailer.shutdown()


def test_known_non_turn_rollout_line_types_are_modeled() -> None:
    for line_type in ("compacted", "inter_agent_communication_metadata"):
        line = rollout_line_adapter.validate_python(
            {
                "timestamp": "2026-07-16T10:00:00Z",
                "type": line_type,
                "payload": {},
            }
        )
        assert line.type == line_type


async def test_compactions_without_turns_are_replay_safe(
    impulses: ObjectSendStream[Impulse],
) -> None:
    records = [
        {
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "compacted",
            "payload": {"message": "Résumé of earlier work"},
        },
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "compacted",
            "payload": {"message": "Résumé of earlier work"},
        },
    ]
    assert not await streamed_runs(impulses, records)
    async with async_session() as session:
        rows = await session.list(
            ExternalCompaction,
            order_bys=[ExternalCompaction["id"]],
            options=[selectinload(ExternalCompaction["messages"])],
        )
    identities = [(row.id, row.messages[0].id) for row in rows]
    assert len(rows) == 2
    assert rows[0].start_offset == 0
    assert rows[1].start_offset == rows[0].end_offset
    assert rows[0].happened_at == datetime(2026, 7, 16, 10, 0, 1, tzinfo=UTC)
    for row in rows:
        assert row.happened_at is not None
        assert row.native_session_id == SESSION_ID
        assert row.native_compaction_id is None
        assert row.trigger == "unknown"
        assert row.source == str(ROLLOUT_LABEL)
        assert row.messages[0].run_id is None
        assert row.messages[0].compaction_id == row.id
        assert row.messages[0].parts == [
            UserPromptPart("Résumé of earlier work", timestamp=row.happened_at)
        ]
    assert not await streamed_runs(impulses, records)
    async with async_session() as session:
        replayed = await session.list(
            ExternalCompaction,
            order_bys=[ExternalCompaction["id"]],
            options=[selectinload(ExternalCompaction["messages"])],
        )
    assert [(row.id, row.messages[0].id) for row in replayed] == identities
    octomate = Octomate()
    thread = await a_loaded_thread(
        octomate.threads, ThreadKey("codex", "thread", SESSION_ID)
    )
    assert not thread.messages


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"type": "compaction", "id": "checkpoint", "encrypted_content": "opaque"},
        {
            "type": "compaction_summary",
            "id": "checkpoint",
            "encrypted_content": "opaque",
        },
        {
            "type": "context_compaction",
            "id": "checkpoint",
            "encrypted_content": "opaque",
        },
        {"type": "context_compaction", "id": "checkpoint"},
    ],
)
async def test_compaction_checkpoints_preserve_opaque_content(
    impulses: ObjectSendStream[Impulse], checkpoint: JsonObject
) -> None:
    assert not await streamed_runs(
        impulses,
        [
            {
                "timestamp": "2026-07-16T10:00:01Z",
                "type": "compacted",
                "payload": {
                    "message": "",
                    "compaction_response_id": "summary-response",
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "retained context"}
                            ],
                        },
                        checkpoint,
                    ],
                },
            }
        ],
    )
    async with async_session() as session:
        rows = await session.list(
            ExternalCompaction,
            options=[selectinload(ExternalCompaction["messages"])],
        )
    assert len(rows) == 1
    assert len(rows[0].messages) == 1
    response = rows[0].messages[0]
    assert isinstance(response, ModelResponse)
    assert response.provider_response_id == "summary-response"
    assert response.run_id is None
    assert response.parts == [
        CompactionPart(
            id="checkpoint",
            provider_name="openai_codex",
            provider_details={"encrypted_content": "opaque"}
            if "encrypted_content" in checkpoint
            else None,
        )
    ]


@pytest.mark.parametrize("driven", [False, True])
async def test_compactions_survive_turn_replay_and_use_the_runtime_owner(
    impulses: ObjectSendStream[Impulse], driven: bool
) -> None:
    octomate = Octomate()
    owner = None
    if driven:
        owner = await octomate.conversations.ensure(
            await a_thread(), agent_tentacle_id="codex"
        )
        await octomate.conversations.record_agent_run(
            owner,
            run_id=TURN_ID,
            messages=[
                ModelRequest(parts=[UserPromptPart("original prompt")]),
                ModelResponse(parts=[TextPart("original answer")]),
            ],
            native_turn_id=TURN_ID,
            external_id=SESSION_ID,
        )
    records = [
        event(0, "task_started", turn_id=TURN_ID),
        event(1, "user_message", message="original prompt"),
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "compacted",
            "payload": {"message": "summary inside a turn"},
        },
        answer_item(3, "original answer"),
        event(4, "task_complete", turn_id=TURN_ID),
    ]
    await streamed_runs(impulses, records)
    await streamed_runs(impulses, records)
    async with async_session() as session:
        rows = await session.list(ExternalCompaction)
    assert len(rows) == 1
    if owner is not None:
        assert rows[0].conversation_id == owner.id
    conversation = await octomate.conversations.get(rows[0].conversation_id)
    assert [run.id for run in conversation.runs] == [TURN_ID]
    assert [
        message.message_text
        for message in (await ConversationManager().history(conversation.id))
    ] == [
        "original prompt",
        "original answer",
    ]


async def test_subagent_compactions_exclude_inherited_parent_history(
    impulses: ObjectSendStream[Impulse],
) -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(
        SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, impulses
    )
    await feed_records(tailer, state, [parent_metadata()])
    await feed_records(
        tailer,
        state,
        [
            child_metadata(),
            {
                "timestamp": "2026-07-16T10:00:03Z",
                "type": "compacted",
                "payload": {"message": "inherited parent summary"},
            },
            event(4, "task_started", turn_id=CHILD_TURN_ONE),
            event(5, "task_complete", turn_id=CHILD_TURN_ONE),
            {
                "timestamp": "2026-07-16T10:00:06Z",
                "type": "compacted",
                "payload": {"message": "child summary"},
            },
        ],
        key="rollout-child.jsonl",
    )
    tailer.detach_remote(state)
    async with async_session() as session:
        rows = await session.list(
            ExternalCompaction,
            options=[selectinload(ExternalCompaction["messages"])],
        )
    assert len(rows) == 1
    assert rows[0].native_session_id == CHILD_THREAD_ID
    assert rows[0].source == "rollout-child.jsonl"
    assert rows[0].messages[0].message_text == "child summary"
    child = await octomate.conversations.get(rows[0].conversation_id)
    assert child.subagent_id == CHILD_THREAD_ID
    assert not child.runs


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": 7},
        {"message": "partial summary", "replacement_history": [{"type": "compaction"}]},
    ],
)
async def test_malformed_compactions_and_notifications_create_no_records(
    impulses: ObjectSendStream[Impulse], payload: JsonObject
) -> None:
    assert not await streamed_runs(
        impulses,
        [
            event(0, "context_compacted"),
            {
                "timestamp": "2026-07-16T10:00:01Z",
                "type": "compacted",
                "payload": payload,
            },
        ],
    )
    async with async_session() as session:
        assert await session.count(ExternalCompaction) == 0
