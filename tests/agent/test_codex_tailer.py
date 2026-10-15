from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    CommandExecutionStatus,
    ItemCompletedNotification,
    ItemStartedNotification,
    ReasoningTextDeltaNotification,
    ThreadItem,
)
from pydantic_ai.messages import (
    EnqueuedMessagesEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartEndEvent,
    TextPart,
    ThinkingPart,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.events import (
    RunEndedEvent,
    RunStartedEvent,
    StreamEvents,
)
from octomate.nerve import ThreadTopic
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.codex.adapter import CodexRunAccumulator
from octomate.tentacles.codex.tailer import CodexTranscriptTailer, OpenTurn
from octomate.tentacles.codex.transcript import RolloutLine
from octomate.types.json import JsonObject, JsonValue
from tests.agent.test_codex_adapter import command_item, notification
from tests.agent.test_codex_native_ingest import (
    ROLLOUT_LABEL,
    SENDER,
    SESSION_ADDRESS,
    SESSION_ID,
    TURN_ID,
    feed_records,
    write_rollout,
)
from tests.support.managers import a_loaded_thread
from tests.support.nerve import held

NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)


def final_parts(events: Iterable[StreamEvents[str]]) -> list[tuple[str, JsonValue]]:
    """Compare completed content; app-server command wrappers rename rollout args."""
    parts: list[tuple[str, JsonValue]] = []
    for event in events:
        if isinstance(event, PartEndEvent) and isinstance(
            event.part, TextPart | ThinkingPart
        ):
            parts.append((event.part.part_kind, event.part.content))
        elif isinstance(event, FunctionToolCallEvent):
            args = event.part.args_as_dict()
            parts.append(
                (
                    "call",
                    {
                        "id": event.part.tool_call_id,
                        "command": args.get("command", args.get("cmd")),
                        "cwd": args.get("cwd", args.get("workdir")),
                    },
                )
            )
        elif isinstance(event, FunctionToolResultEvent):
            assert isinstance(event.part.content, str)
            parts.append(
                (
                    "result",
                    {"id": event.part.tool_call_id, "content": event.part.content},
                )
            )
    return parts


def test_rollout_parts_match_the_completed_driven_parts() -> None:
    driven = CodexRunAccumulator()
    events: list[StreamEvents[str]] = []
    for delta in ("check", "ing"):
        events.extend(
            driven.consume(
                notification(
                    "item/reasoning/delta",
                    ReasoningTextDeltaNotification(
                        content_index=0,
                        delta=delta,
                        item_id="reasoning-1",
                        thread_id=SESSION_ID,
                        turn_id=TURN_ID,
                    ),
                )
            )
        )
    events.extend(
        driven.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=1,
                    thread_id=SESSION_ID,
                    turn_id=TURN_ID,
                    item=ThreadItem.model_validate(
                        {
                            "type": "reasoning",
                            "id": "reasoning-1",
                            "content": ["checking"],
                            "summary": [],
                        }
                    ),
                ),
            )
        )
    )
    events.extend(
        driven.consume(
            notification(
                "item/started",
                ItemStartedNotification(
                    started_at_ms=1,
                    thread_id=SESSION_ID,
                    turn_id=TURN_ID,
                    item=command_item(status=CommandExecutionStatus.in_progress),
                ),
            )
        )
    )
    events.extend(
        driven.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=2,
                    thread_id=SESSION_ID,
                    turn_id=TURN_ID,
                    item=command_item(
                        status=CommandExecutionStatus.completed,
                        aggregated_output="ok",
                        exit_code=0,
                    ),
                ),
            )
        )
    )
    for delta in ("All ", "done"):
        events.extend(
            driven.consume(
                notification(
                    "item/agentMessage/delta",
                    AgentMessageDeltaNotification(
                        delta=delta,
                        item_id="msg-1",
                        thread_id=SESSION_ID,
                        turn_id=TURN_ID,
                    ),
                )
            )
        )
    events.extend(
        driven.consume(
            notification(
                "item/completed",
                ItemCompletedNotification(
                    completed_at_ms=3,
                    thread_id=SESSION_ID,
                    turn_id=TURN_ID,
                    item=ThreadItem.model_validate(
                        {
                            "type": "agentMessage",
                            "id": "msg-1",
                            "text": "All done",
                            "phase": "final_answer",
                        }
                    ),
                ),
            )
        )
    )

    turn = OpenTurn(TURN_ID, 0, 0, NOW, "local")
    payloads: list[JsonObject] = [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "checking"}],
        },
        {
            "type": "function_call",
            "call_id": "cmd-1",
            "name": "exec_command",
            "arguments": '{"cmd":"pytest","workdir":"/repo"}',
        },
        {"type": "function_call_output", "call_id": "cmd-1", "output": "ok"},
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "All done"}],
        },
    ]
    tailed = [
        event
        for payload in payloads
        for event in CodexTranscriptTailer.consume_response_item(
            turn,
            RolloutLine(timestamp=NOW, type="response_item", payload=payload),
        )
    ]
    assert [event.event_kind for event in tailed] == [
        "part_start",
        "part_end",
        "function_tool_call",
        "function_tool_result",
        "part_start",
        "part_end",
    ]
    assert final_parts(tailed) == final_parts(events)
    assert [part.part_kind for message in turn.messages for part in message.parts] == [
        "thinking",
        "builtin-tool-call",
        "builtin-tool-return",
        "text",
    ]


@pytest.fixture
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.mark.usefixtures("db")
async def test_live_turn_restarts_then_settles_and_committed_replay_is_silent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    records = path.read_text().splitlines()
    octomate = Octomate()
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.threads)
    key = ThreadKey.from_address(SESSION_ADDRESS)
    with octomate.nerve.subscribe(ThreadTopic(key)) as live:
        for finished in (False, True, True):
            async with octomate.nerve.publish(SESSION_ADDRESS) as publisher:
                state, _ = await tailer.attach_remote(
                    SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, publisher
                )
                offset = 0
                for raw in records if finished else records[:-1]:
                    end = offset + len(raw.encode()) + 1
                    await tailer.feed_remote(state, None, raw, offset, end)
                    offset = end
                tailer.detach_remote(state)
            events = held(live)
            if finished and not events:
                break
            started = events[0].event
            assert isinstance(started, RunStartedEvent)
            assert started.sender == SENDER
            prompt = events[1].event
            assert isinstance(prompt, EnqueuedMessagesEvent)
            thread = await a_loaded_thread(octomate.threads, key)
            assert [
                str(row.id) for row in thread.messages if row.direction == "inbound"
            ] == [prompt.enqueue_id]
            if finished:
                ended = events[-1].event
                assert isinstance(ended, RunEndedEvent)
                assert ended.thread_message_id == next(
                    row.id for row in thread.messages if row.direction == "outbound"
                )
            else:
                assert all(event.event.event_kind != "run_ended" for event in events)
        else:
            pytest.fail("a committed replay published events")
    assert not octomate.awakes


@pytest.mark.usefixtures("db")
@pytest.mark.parametrize("fail_at", ["run", "ledger"])
async def test_failed_commit_never_publishes_run_ended(
    monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    octomate = Octomate()
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.threads)
    owner, method = (
        (octomate.conversations, "record_external_run")
        if fail_at == "run"
        else (octomate.threads, "bind_native_run")
    )
    monkeypatch.setattr(
        owner, method, AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    with octomate.nerve.subscribe(
        ThreadTopic(ThreadKey.from_address(SESSION_ADDRESS))
    ) as live:
        async with octomate.nerve.publish(SESSION_ADDRESS) as publisher:
            state, _ = await tailer.attach_remote(
                SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, publisher
            )
            with pytest.raises(RuntimeError, match="commit failed"):
                await feed_records(
                    tailer,
                    state,
                    [
                        {
                            "timestamp": NOW.isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": TURN_ID},
                        },
                        {
                            "timestamp": NOW.isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "hello"},
                        },
                        {
                            "timestamp": NOW.isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": TURN_ID},
                        },
                    ],
                )
            tailer.detach_remote(state)
        assert all(impulse.event.event_kind != "run_ended" for impulse in held(live))


@pytest.mark.usefixtures("db")
async def test_empty_reserved_turn_commits_its_checkpoint_and_settles() -> None:
    octomate = Octomate()
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.threads)
    with octomate.nerve.subscribe(
        ThreadTopic(ThreadKey.from_address(SESSION_ADDRESS))
    ) as live:
        async with octomate.nerve.publish(SESSION_ADDRESS) as publisher:
            state, _ = await tailer.attach_remote(
                SESSION_ID, ROLLOUT_LABEL, SENDER, SESSION_ADDRESS, publisher
            )
            assert state.conversation is not None
            await octomate.conversations.record_external_run(
                state.conversation, run_id=TURN_ID, native_session_id=SESSION_ID
            )
            await feed_records(
                tailer,
                state,
                [
                    {
                        "timestamp": NOW.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": TURN_ID},
                    },
                    {
                        "timestamp": NOW.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": TURN_ID},
                    },
                ],
            )
            stored = await octomate.conversations.get(state.conversation.id)
            assert stored.native_run_ids == {TURN_ID}
            assert not stored.messages
            tailer.detach_remote(state)
        assert [impulse.event.event_kind for impulse in held(live)] == [
            "run_started",
            "run_ended",
        ]
