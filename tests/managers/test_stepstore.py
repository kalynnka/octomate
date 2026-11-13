"""The upstream StepStore contract on the service's real Arcanus persistence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    CompactionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    RunRecord,
    StepEvent,
    StepPersistence,
    ToolEffectRecord,
)
from pydantic_ai_harness.step_persistence import (
    StepStore as StepStoreProtocol,
)
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.steps import StepStore, message_adapter
from octomate.schemas.conversation import Conversation, ConversationRun
from octomate.schemas.messages import ModelMessage as ArchiveMessage
from octomate.schemas.messages import ModelRequest as ArchiveRequest
from octomate.schemas.messages import ModelResponse as ArchiveResponse
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.schemas.steps import (
    Snapshot,
    SnapshotMembership,
    SnapshotMessage,
    ToolEffect,
)
from octomate.schemas.steps import (
    StepEvent as StoredStepEvent,
)
from tests.support.managers import a_thread

NOW = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.fixture
async def conversation(in_memory_engine: AsyncEngine) -> Conversation:
    return await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="inkling"
    )


@pytest.fixture
async def run(conversation: Conversation) -> RunRecord:
    record = RunRecord(
        run_id=str(uuid7()),
        conversation_id=str(conversation.id),
        agent_name="inkling",
        metadata={"purpose": "test"},
        registration_id=str(uuid7()),
        started_at=NOW,
    )
    store: StepStoreProtocol = StepStore()
    await store.register_run(record)
    return record


async def test_registration_uses_the_shared_run_and_conversation(
    run: RunRecord,
) -> None:
    store = StepStore()
    assert await store.get_run(run_id=run.run_id) == run
    async with async_session() as session:
        assert await session.count(AgentRun) == 1
        assert await session.count(ArchiveMessage) == 0
        [link] = await session.list(ConversationRun)
        assert link.run_id == run.run_id
        assert str(link.conversation_id) == run.conversation_id
        stored = await session.get(AgentRun, run.run_id)
        assert stored is not None
        assert stored.name is None
        assert stored.agent_name == run.agent_name
        assert stored.metadata == run.metadata
        assert stored.registration_id == run.registration_id


async def test_registration_refuses_to_overwrite_an_existing_run(
    run: RunRecord,
) -> None:
    store = StepStore()
    with pytest.raises(IntegrityError):
        await store.register_run(replace(run, metadata={"replaced": "no"}))
    assert await store.get_run(run_id=run.run_id) == run


@pytest.mark.parametrize("metadata", [{}, {"purpose": "archive"}])
async def test_archive_completes_a_registered_run(
    conversation: Conversation,
    metadata: dict[str, str],
    in_memory_engine: AsyncEngine,
) -> None:
    record = RunRecord(
        run_id=str(uuid7()),
        conversation_id=str(conversation.id),
        parent_run_id=str(uuid7()),
        agent_name="inkling",
        metadata=metadata,
        registration_id=str(uuid7()),
        started_at=NOW,
    )
    store = StepStore()
    await store.register_run(record)
    manager = ConversationManager()
    assert await manager.record_agent_run(conversation, record.run_id, []) is None
    messages = [
        ModelRequest(parts=[UserPromptPart("question")]),
        ModelResponse(parts=[TextPart("answer")]),
    ]
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        archived = await manager.record_agent_run(
            conversation,
            record.run_id,
            messages,
            name="react",
            model_name="test-model",
            context_window=100_000,
            context_used=42,
            permission_mode="default",
            cwd=Path("/workspace"),
        )
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    selects = [
        call.args[2]
        for call in queries.call_args_list
        if call.args[2].startswith("SELECT")
    ]
    assert len(selects) == 2
    assert all("model_messages.parts" not in query for query in selects)
    assert archived is not None
    assert await store.get_run(run_id=record.run_id) == record
    async with async_session() as session:
        assert await session.count(AgentRun) == 1
        assert await session.count(ConversationRun) == 1
        assert await session.count(ArchiveMessage) == 2
        stored = await session.get(AgentRun, record.run_id)
        assert stored is not None
        assert stored.name == "react"
        assert stored.model_name == "test-model"
        assert stored.context_window == 100_000
        assert stored.context_used == 42
        assert stored.permission_mode == "default"
        assert stored.cwd == Path("/workspace")
        assert [message.id for message in stored.messages] == [
            message.id for message in archived.messages
        ]
        assert [message.parts for message in stored.messages] == [
            message.parts for message in messages
        ]
        assert all(message.run_id == record.run_id for message in stored.messages)
    with pytest.raises(IntegrityError):
        await manager.record_agent_run(conversation, record.run_id, messages)
    assert await store.get_run(run_id=record.run_id) == record
    async with async_session() as session:
        assert await session.count(ArchiveMessage) == 2


@pytest.mark.parametrize("ownership", ["conversation", "parent"])
async def test_archive_cannot_reassign_a_registered_run(
    run: RunRecord,
    conversation: Conversation,
    ownership: Literal["conversation", "parent"],
) -> None:
    manager = ConversationManager()
    target = conversation
    if ownership == "conversation":
        target = await manager.ensure(
            await a_thread("other"), agent_tentacle_id="inkling"
        )
        await manager.fork(conversation, target)
    with pytest.raises(ValueError, match=ownership):
        await manager.record_agent_run(
            target,
            run.run_id,
            [ModelResponse(parts=[TextPart("wrong owner")])],
            parent_run_id=str(uuid7()) if ownership == "parent" else None,
        )
    assert await StepStore().get_run(run_id=run.run_id) == run
    async with async_session() as session:
        assert await session.count(ArchiveMessage) == 0


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize(("context_window", "context_used"), [(0, None), (None, -1)])
async def test_archive_validates_run_updates_before_persisting(
    run: RunRecord,
    conversation: Conversation,
    registered: bool,
    context_window: int | None,
    context_used: int | None,
) -> None:
    with pytest.raises(ValidationError):
        await ConversationManager().record_agent_run(
            conversation,
            run.run_id if registered else str(uuid7()),
            [ModelResponse(parts=[TextPart("invalid context usage")])],
            name="must not be saved",
            context_window=context_window,
            context_used=context_used,
        )
    async with async_session() as session:
        assert await session.count(AgentRun) == 1
        assert await session.count(ArchiveMessage) == 0
        stored = await session.get(AgentRun, run.run_id)
        assert stored is not None
        assert stored.name is None
        assert stored.context_window is None
        assert stored.context_used is None


@pytest.mark.parametrize("conversation_id", [None, "not-a-conversation"])
async def test_registration_requires_a_conversation_identity(
    conversation: Conversation, conversation_id: str | None
) -> None:
    with pytest.raises(ValidationError):
        await StepStore().register_run(
            RunRecord(run_id=str(uuid7()), conversation_id=conversation_id)
        )
    async with async_session() as session:
        assert await session.count(AgentRun) == 0


async def test_registration_requires_an_existing_conversation(
    conversation: Conversation,
) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await StepStore().register_run(
            RunRecord(run_id=str(uuid7()), conversation_id=str(uuid7()))
        )
    async with async_session() as session:
        assert await session.count(AgentRun) == 0


@pytest.mark.parametrize("run_type", [AgentRun, ExternalAgentRun])
async def test_unregistered_runs_are_not_exposed_as_step_registrations(
    run: RunRecord, conversation: Conversation, run_type: type[AgentRun]
) -> None:
    ordinary = run_type(id=str(uuid7()), conversation_id=conversation.id)
    async with async_session() as session:
        session.add(ordinary)
        await session.commit()
        assert list(
            (
                await session.scalars(
                    select(AgentRun["id"]).where(AgentRun["metadata"].is_(None))
                )
            ).all()
        ) == [ordinary.id]
    store = StepStore()
    assert await store.get_run(run_id=ordinary.id) is None
    assert await store.list_runs() == [run]


async def test_registration_can_have_empty_metadata_and_no_retry_token(
    run: RunRecord,
) -> None:
    record = replace(
        run, run_id=str(uuid7()), metadata={}, registration_id=None, agent_name=None
    )
    store = StepStore()
    await store.register_run(record)
    assert await store.get_run(run_id=record.run_id) == record
    assert await store.list_runs() == [run, record]


async def test_upstream_refuses_a_different_registration_for_the_same_run(
    run: RunRecord,
) -> None:
    store = StepStore()
    agent = Agent(
        TestModel(), capabilities=[StepPersistence(store=store, run_id=run.run_id)]
    )
    with pytest.raises(ValueError, match="already in the store"):
        await agent.run("do not overwrite", conversation_id=run.conversation_id)
    assert await store.get_run(run_id=run.run_id) == run


async def test_run_filters_combine_and_sort_by_start_time(run: RunRecord) -> None:
    store = StepStore()
    other = await ConversationManager().ensure(
        await a_thread("other"), agent_tentacle_id="inkling"
    )
    later = replace(
        run,
        run_id=str(uuid7()),
        parent_run_id=run.run_id,
        started_at=NOW + timedelta(seconds=2),
    )
    earlier = replace(later, run_id=str(uuid7()), started_at=NOW + timedelta(seconds=1))
    elsewhere = replace(later, run_id=str(uuid7()), conversation_id=str(other.id))
    for record in (later, earlier, elsewhere):
        await store.register_run(record)
    assert await store.list_runs(
        parent_run_id=run.run_id, conversation_id=run.conversation_id
    ) == [earlier, later]
    assert await store.list_runs(conversation_id=str(other.id)) == [elsewhere]
    assert await store.list_runs(parent_run_id=str(uuid7())) == []
    assert await store.get_run(run_id=str(uuid7())) is None


async def test_events_preserve_append_order_and_suppress_only_keyed_retries(
    run: RunRecord,
) -> None:
    store = StepStore()
    first = StepEvent(
        run_id=run.run_id,
        kind="run_started",
        step_index=4,
        timestamp=NOW,
        metadata={"event": "first"},
        idempotency_key="one",
    )
    second = replace(
        first,
        kind="run_failed",
        timestamp=NOW - timedelta(seconds=1),
        idempotency_key=None,
        error="stopped",
    )
    await store.append_event(first)
    await store.append_event(second)
    await store.append_event(
        replace(first, error="retry must not replace the original")
    )
    await store.append_event(second)
    assert await store.list_events(run_id=run.run_id) == [first, second, second]
    assert await store.list_events(run_id=str(uuid7())) == []


async def test_event_reads_are_not_limited_to_one_hundred(run: RunRecord) -> None:
    store = StepStore()
    for index in range(101):
        await store.append_event(
            StepEvent(run_id=run.run_id, kind="model_request_started", step_index=index)
        )
    assert len(await store.list_events(run_id=run.run_id)) == 101


async def test_retried_writes_are_atomic_across_store_instances(run: RunRecord) -> None:
    boundary = StepEvent(
        run_id=run.run_id, kind="run_started", step_index=0, idempotency_key="event"
    )
    snapshot = ContinuableSnapshot(
        run_id=run.run_id,
        step_index=0,
        messages=[ModelResponse(parts=[TextPart("saved")])],
        idempotency_key="snapshot",
    )
    await asyncio.gather(*(StepStore().append_event(boundary) for _ in range(3)))
    await asyncio.gather(*(StepStore().save_snapshot(snapshot) for _ in range(3)))
    async with async_session() as session:
        assert await session.count(StoredStepEvent) == 1
        assert await session.count(Snapshot) == 1
        assert await session.count(SnapshotMembership) == 1
        assert await session.count(SnapshotMessage) == 1


async def test_empty_checkpoint_is_distinct_from_no_checkpoint(run: RunRecord) -> None:
    store = StepStore()
    assert await store.latest_snapshot(run_id=run.run_id) is None
    snapshot = ContinuableSnapshot(run_id=run.run_id, step_index=0, messages=[])
    await store.save_snapshot(snapshot)
    assert await store.latest_snapshot(run_id=run.run_id) == snapshot


async def test_latest_checkpoint_uses_write_order_and_keeps_complete_context(
    run: RunRecord,
) -> None:
    store = StepStore()
    settled = ContinuableSnapshot(
        run_id=run.run_id,
        step_index=4,
        messages=[ModelResponse(parts=[TextPart("settled")])],
        timestamp=NOW,
        idempotency_key="first",
    )
    interrupted = replace(
        settled,
        step_index=2,
        state="interrupted",
        timestamp=NOW - timedelta(seconds=5),
        idempotency_key="second",
    )
    await store.save_snapshot(settled)
    await store.save_snapshot(interrupted)
    await store.save_snapshot(settled)
    assert await store.latest_snapshot(run_id=run.run_id) == settled
    assert (
        await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
        == interrupted
    )
    async with async_session() as session:
        assert await session.count(Snapshot) == 2


async def test_only_interrupted_checkpoint_requires_opt_in(run: RunRecord) -> None:
    store = StepStore()
    snapshot = ContinuableSnapshot(
        run_id=run.run_id, step_index=1, messages=[], state="interrupted"
    )
    await store.save_snapshot(snapshot)
    assert await store.latest_snapshot(run_id=run.run_id) is None
    assert (
        await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
        == snapshot
    )


async def test_compacted_context_keeps_old_versions_and_repeated_references(
    run: RunRecord,
) -> None:
    store = StepStore()
    prompt = ModelRequest(parts=[UserPromptPart("original")])
    part = TextPart("retained")
    answer = ModelResponse(parts=[part])
    first = ContinuableSnapshot(
        run_id=run.run_id, step_index=1, messages=[prompt, answer]
    )
    await store.save_snapshot(first)
    summary = ModelResponse(parts=[CompactionPart(content="summary")])
    compacted = replace(first, messages=[summary, answer, answer])
    await store.save_snapshot(compacted)
    assert await StepStore().latest_snapshot(run_id=run.run_id) == compacted
    part.content = "later edit"
    await store.save_snapshot(replace(first, messages=[answer]))
    async with async_session() as session:
        assert await session.count(SnapshotMessage) == 4
        assert await session.count(SnapshotMembership) == 6
        contents = list(
            (await session.scalars(select(SnapshotMessage["content"]))).all()
        )
        assert (
            message_adapter.dump_json(
                ModelResponse(parts=[TextPart("retained")], timestamp=answer.timestamp)
            )
            in contents
        )
        assert await session.count(ArchiveMessage) == 0


async def test_checkpoint_round_trips_binary_content(run: RunRecord) -> None:
    snapshot = ContinuableSnapshot(
        run_id=run.run_id,
        step_index=1,
        messages=[
            ModelRequest(
                parts=[
                    UserPromptPart(
                        [BinaryContent(data=b"\xff\x00\xfe", media_type="image/png")]
                    )
                ]
            )
        ],
    )
    await StepStore().save_snapshot(snapshot)
    restored = await StepStore().latest_snapshot(run_id=run.run_id)
    assert restored is not None
    [message] = restored.messages
    assert isinstance(message, ModelRequest)
    [part] = message.parts
    assert isinstance(part, UserPromptPart)
    assert not isinstance(part.content, str)
    [content] = part.content
    assert isinstance(content, BinaryContent)
    assert content.data == b"\xff\x00\xfe"
    assert content.media_type == "image/png"


async def test_failed_checkpoint_does_not_leave_metadata_without_content(
    run: RunRecord,
) -> None:
    snapshot = ContinuableSnapshot(
        run_id=run.run_id,
        step_index=1,
        messages=[ModelResponse(parts=[TextPart("fail")])],
    )
    with (
        patch.object(
            message_adapter,
            "dump_json",
            side_effect=RuntimeError("serialization failed"),
        ),
        pytest.raises(RuntimeError, match="serialization failed"),
    ):
        await StepStore().save_snapshot(snapshot)
    async with async_session() as session:
        assert await session.count(Snapshot) == 0
        assert await session.count(SnapshotMessage) == 0


async def test_tool_effects_update_in_place_and_are_scoped_to_runs(
    run: RunRecord,
) -> None:
    store = StepStore()
    other = replace(run, run_id=str(uuid7()))
    await store.register_run(other)
    started = ToolEffectRecord(
        run_id=run.run_id,
        tool_call_id="call",
        tool_name="write",
        status="started",
        started_at=NOW,
        idempotency_key="effect",
    )
    elsewhere = replace(started, run_id=other.run_id)
    await asyncio.gather(*(StepStore().record_tool_effect(started) for _ in range(3)))
    await store.record_tool_effect(elsewhere)
    assert await store.list_unresolved_tool_effects(run_id=run.run_id) == [started]
    completed = replace(
        started,
        status="completed",
        ended_at=NOW + timedelta(seconds=1),
        effect_summary="file written",
    )
    await store.record_tool_effect(completed)
    assert (
        await store.get_tool_effect(run_id=run.run_id, tool_call_id="call") == completed
    )
    assert await store.list_unresolved_tool_effects(run_id=run.run_id) == []
    assert await store.list_unresolved_tool_effects(run_id=other.run_id) == [elsewhere]
    assert (
        await store.get_tool_effect(run_id=other.run_id, tool_call_id="missing") is None
    )
    async with async_session() as session:
        assert await session.count(ToolEffect) == 2


async def test_checkpoint_queries_do_not_grow_per_message(
    run: RunRecord, in_memory_engine: AsyncEngine
) -> None:
    store = StepStore()
    statements = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", statements)
    try:
        for size in (1, 1000):
            messages: list[ModelMessage] = [
                ModelResponse(parts=[TextPart(str(index))]) for index in range(size)
            ]
            checkpoint = ContinuableSnapshot(
                run_id=run.run_id,
                step_index=1,
                messages=messages,
                idempotency_key=str(size),
            )
            statements.reset_mock()
            await store.save_snapshot(checkpoint)
            assert statements.call_count == 4
            statements.reset_mock()
            await store.save_snapshot(checkpoint)
            assert statements.call_count == 1
            statements.reset_mock()
            await store.save_snapshot(
                replace(checkpoint, idempotency_key=f"{size}-reuse")
            )
            assert statements.call_count == 3
            statements.reset_mock()
            snapshot = await store.latest_snapshot(run_id=run.run_id)
            assert snapshot is not None
            assert snapshot.messages == messages
            assert statements.call_count == 2
        statements.reset_mock()
        assert await store.get_run(run_id=run.run_id) == run
        assert await store.list_runs() == [run]
        assert statements.call_count == 2
        assert all(
            "model_messages" not in call.args[2] for call in statements.call_args_list
        )
        assert all(" JOIN " not in call.args[2] for call in statements.call_args_list)
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", statements)


async def test_checkpoints_preserve_archive_and_compaction_ownership(
    run: RunRecord, conversation: Conversation
) -> None:
    manager = ConversationManager()
    archive = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("original")]),
            ModelResponse(parts=[TextPart("retained")]),
        ],
    )
    assert archive is not None
    retained = archive.messages[1]
    summary = ArchiveResponse(parts=[CompactionPart(content="summary")])
    revision = await manager.record_context_compaction(
        conversation,
        before=list(archive.messages),
        after=[summary, retained],
        trigger="auto",
    )
    assert revision is not None
    assert summary.compaction_id is not None
    snapshot = ContinuableSnapshot(
        run_id=run.run_id,
        step_index=1,
        messages=[summary, retained],
        conversation_id=run.conversation_id,
    )
    await StepStore().save_snapshot(snapshot)
    restored = await StepStore().latest_snapshot(run_id=run.run_id)
    assert restored is not None
    assert [message.parts for message in restored.messages] == [
        summary.parts,
        retained.parts,
    ]
    async with async_session() as session:
        assert await session.count(ArchiveMessage) == 3
        original = await session.get(ArchiveRequest, archive.messages[0].id)
        assert original is not None
        assert original.run_id == archive.id
        stored = await session.get(ArchiveResponse, retained.id)
        assert stored is not None
        assert stored.run_id == archive.id
        assert stored.compaction_id is None
        replacement = await session.get(ArchiveResponse, summary.id)
        assert replacement is not None
        assert replacement.run_id is None
        assert replacement.compaction_id == summary.compaction_id


async def test_upstream_step_persistence_uses_the_store_across_runs(
    conversation: Conversation,
) -> None:
    def lookup() -> str:
        return "stored tool result"

    store = StepStore()
    agent = Agent(
        TestModel(), tools=[lookup], capabilities=[StepPersistence(store=store)]
    )
    first = await agent.run("look it up", conversation_id=str(conversation.id))
    manager = ConversationManager()
    await manager.record_agent_run(conversation, first.run_id, first.new_messages())
    first_snapshot = await store.latest_snapshot(run_id=first.run_id)
    assert first_snapshot is not None
    assert first_snapshot.messages == first.all_messages()
    second = await agent.run(
        "continue",
        conversation_id=str(conversation.id),
        message_history=first_snapshot.messages,
    )
    await manager.record_agent_run(conversation, second.run_id, second.new_messages())
    records = await store.list_runs(conversation_id=str(conversation.id))
    assert [record.run_id for record in records] == [first.run_id, second.run_id]
    assert await store.latest_snapshot(run_id=first.run_id) == first_snapshot
    events = await store.list_events(run_id=first.run_id)
    assert events[0].kind == "run_started"
    assert events[-1].kind == "run_completed"
    tool_event = next(entry for entry in events if entry.kind == "tool_call_completed")
    assert tool_event.tool_call_id is not None
    effect = await store.get_tool_effect(
        run_id=first.run_id, tool_call_id=tool_event.tool_call_id
    )
    assert effect is not None
    assert effect.status == "completed"
    assert await store.list_unresolved_tool_effects(run_id=first.run_id) == []
    async with async_session() as session:
        assert await session.count(AgentRun) == 2
        assert await session.count(ConversationRun) == 2
        assert await session.count(ArchiveMessage) == len(first.new_messages()) + len(
            second.new_messages()
        )
