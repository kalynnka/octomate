"""Conversation carryover state references the existing run and compaction archive."""

import asyncio
from dataclasses import fields
from decimal import Decimal
from typing import get_type_hints
from unittest.mock import Mock

import pytest
from arcanus.materia.sqlalchemy import AsyncSession, lazyload
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent, AgentRunResult, DeferredToolRequests, RunContext
from pydantic_ai import Conversation as PydanticConversation
from pydantic_ai.conversation import ConversationTypeAdapter
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessagesTypeAdapter,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.messages import ModelRequest as PydanticModelRequest
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import SummarizingCompaction
from pydantic_ai_harness.step_persistence import RunRecord, StepPersistence
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager, ConversationStore
from octomate.managers.steps import StepStore
from octomate.models.conversation import Conversation as ConversationModel
from octomate.schemas.compactions import Compaction, CompactionMembership
from octomate.schemas.conversation import (
    Conversation,
    ConversationCompaction,
    ConversationRun,
)
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from octomate.schemas.runs import AgentRun
from octomate.tentacles.inkling.capabilities.history import History
from tests.support.managers import a_thread


@pytest.fixture
async def conversation(in_memory_engine: AsyncEngine) -> Conversation:
    return await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="inkling"
    )


async def test_store_loads_effective_context_and_carryover(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("old question")])],
    )
    assert run is not None
    context = await manager.get(conversation.id, with_runs=False, with_messages=True)
    context.usage = RunUsage(requests=2, input_tokens=42)
    context.deferred_tool_requests = DeferredToolRequests(
        approvals=[ToolCallPart("confirm", {}, "approval-1")]
    )
    await store.record_conversation(context)
    summary = ModelRequest(parts=[CompactionPart(content="summary")])
    await manager.record_context_compaction(
        context, before=list(run.messages), after=[summary], trigger="auto"
    )

    loaded = await store.load(str(conversation.id))
    assert loaded is not None
    assert [message.id for message in loaded.messages] == [summary.id]
    assert loaded.new_message_index == 1
    assert loaded.usage == context.usage
    assert loaded.deferred_tool_requests == context.deferred_tool_requests
    assert loaded.revision != context.revision
    assert not loaded.runs.loaded
    assert await store.load(str(uuid7())) is None
    with pytest.raises(ValidationError):
        await store.load("not-a-uuid")


async def test_store_load_does_not_hide_invalid_compaction_history(
    conversation: Conversation,
) -> None:
    async with async_session() as session:
        session.add(Compaction(conversation_id=conversation.id))
        await session.commit()
    with pytest.raises(ValueError, match="every compaction to be linked"):
        await ConversationStore(ConversationManager()).load(str(conversation.id))


async def test_unsupported_store_save_does_not_write(
    conversation: Conversation, in_memory_engine: AsyncEngine
) -> None:
    conversation.messages.append(
        ModelRequest(run_id=str(uuid7()), parts=[UserPromptPart("new question")])
    )
    store = ConversationStore(ConversationManager())
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        with pytest.raises(NotImplementedError, match="record heads"):
            await store.save(conversation)
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 0
    assert conversation.revision is None
    assert conversation.new_message_index is None


@pytest.mark.parametrize(("limit", "scope"), [(1, None), (None, "owner")])
async def test_store_rejects_unsupported_load_options(
    conversation: Conversation, limit: int | None, scope: str | None
) -> None:
    with pytest.raises(NotImplementedError, match="Scoped and windowed"):
        await ConversationStore(ConversationManager()).load(
            str(conversation.id), limit=limit, scope=scope
        )


@pytest.mark.parametrize("with_runs", [False, True])
@pytest.mark.parametrize("with_messages", [False, True])
async def test_get_loads_runs_and_effective_context_independently(
    conversation: Conversation,
    in_memory_engine: AsyncEngine,
    with_runs: bool,
    with_messages: bool,
) -> None:
    manager = ConversationManager()
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("old question")])],
    )
    assert run is not None
    summary = ModelRequest(parts=[CompactionPart(content="summary")])
    await manager.record_context_compaction(
        conversation, before=list(run.messages), after=[summary], trigger="auto"
    )

    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        loaded = await manager.get(
            conversation.id, with_runs=with_runs, with_messages=with_messages
        )
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)

    assert loaded.runs.loaded is with_runs
    assert [message.id for message in loaded.messages] == (
        [summary.id] if with_messages else []
    )
    assert loaded.new_message_index == (1 if with_messages else None)
    if with_runs:
        assert [message.id for message in loaded.runs[0].messages] == [
            message.id for message in run.messages
        ]
    elif not with_messages:
        assert queries.call_count == 1


def test_upstream_bookkeeping_is_inherited_without_message_or_cursor_columns() -> None:
    upstream = {field.name: field for field in fields(PydanticConversation)}
    mapped = {field.name: field for field in fields(Conversation)}
    upstream_types = get_type_hints(PydanticConversation)
    mapped_types = get_type_hints(Conversation)
    for name in ("usage", "revision", "new_message_index", "deferred_tool_requests"):
        assert mapped_types[name] == upstream_types[name]
        assert mapped[name].default == upstream[name].default
        assert mapped[name].compare == upstream[name].compare
    assert "messages" not in ConversationModel.__table__.columns
    assert "new_message_index" not in ConversationModel.__table__.columns
    assert "messages" not in ConversationModel.__mapper__.attrs
    assert "new_message_index" not in ConversationModel.__mapper__.attrs
    assert "all_messages" not in ConversationModel.__mapper__.relationships
    assert "archive" not in ConversationModel.__mapper__.relationships
    assert not hasattr(ConversationModel, "messages")
    assert not hasattr(ConversationModel, "new_message_index")
    assert "all_messages" not in Conversation.__pydantic_fields__
    assert "history" not in Conversation.__pydantic_fields__


async def test_history_is_explicit_and_limits_the_database_read(
    conversation: Conversation, in_memory_engine: AsyncEngine
) -> None:
    manager = ConversationManager()
    assert not await manager.has_history(conversation.id)
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("original")]),
            ModelResponse(parts=[TextPart("original answer")]),
            ModelRequest(parts=[UserPromptPart("recent")]),
        ],
    )
    assert run is not None
    summary = ModelRequest(parts=[CompactionPart(content="summary")])
    await manager.record_context_compaction(
        conversation, before=list(run.messages), after=[summary], trigger="auto"
    )
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        tail = await manager.history(conversation.id, limit=2)
        assert queries.call_count == 1
        assert "LIMIT" in queries.call_args.args[2]
        assert await manager.has_history(conversation.id)
        assert queries.call_count == 2
        assert "EXISTS" in queries.call_args.args[2]
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert [message.id for message in tail] == [
        message.id for message in run.messages[-2:]
    ]
    assert await manager.history(conversation.id, limit=0) == []
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert [message.id for message in loaded.messages] == [summary.id]
    assert [message.id for message in await manager.history(conversation.id)] == [
        message.id for message in run.messages
    ]


async def test_runtime_context_validates_serializes_and_revalidates(
    conversation: Conversation,
) -> None:
    adapter = TypeAdapter(Conversation)
    runtime = adapter.validate_python(
        {
            "thread_id": conversation.thread_id,
            "agent_tentacle_id": "inkling",
            "messages": [{"kind": "request", "parts": [UserPromptPart("runtime")]}],
            "new_message_index": "1",
        }
    )
    assert isinstance(runtime.messages[0], ModelRequest)
    assert runtime.new_message_index == 1
    provider = runtime.__transmuter_provided__
    assert provider is not None
    assert not hasattr(provider, "messages")
    assert not hasattr(provider, "new_message_index")
    assert adapter.dump_python(runtime, mode="json", include={"new_message_index"}) == {
        "new_message_index": 1
    }
    upstream = ConversationTypeAdapter.validate_json(
        adapter.dump_json(runtime, include={"messages", "new_message_index"})
    )
    assert upstream.messages[0].parts == runtime.messages[0].parts
    assert upstream.new_message_index == 1
    message = runtime.messages[0]
    assert runtime.revalidate() is runtime
    assert runtime.messages[0] is message
    assert runtime.new_message_index == 1
    assert runtime.__transmuter_provided__ is provider

    with pytest.raises(ValidationError, match="messages"):
        adapter.validate_python(
            {
                "thread_id": conversation.thread_id,
                "agent_tentacle_id": "inkling",
                "messages": "invalid",
            }
        )
    with pytest.raises(ValidationError, match="new_message_index"):
        adapter.validate_python(
            {
                "thread_id": conversation.thread_id,
                "agent_tentacle_id": "inkling",
                "new_message_index": "invalid",
            }
        )
    for name in ("messages", "new_message_index"):
        with pytest.raises(KeyError, match=f"Transient field '{name}'"):
            Conversation[name]


async def test_listing_serializes_prefetched_runs_without_reading_messages(
    conversation: Conversation, in_memory_engine: AsyncEngine
) -> None:
    manager = ConversationManager()
    peer = await manager.ensure(conversation.thread_id, agent_tentacle_id="codex")
    expected: dict[str, list[str]] = {}
    for owner in (conversation, peer):
        run = await manager.record_agent_run(
            owner, str(uuid7()), [ModelRequest(parts=[UserPromptPart("archived")])]
        )
        assert run is not None
        expected[str(owner.id)] = [run.id]
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        listed = await manager.for_thread(conversation.thread_id)
        payload = TypeAdapter(list[Conversation]).dump_python(
            listed, mode="json", exclude={"__all__": {"messages"}}
        )
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert {
        row["id"]: [run["id"] for run in row["runs"]] for row in payload
    } == expected
    assert queries.call_count == 2
    for call in queries.call_args_list:
        assert call.args[2].startswith("SELECT")
        assert "model_messages" not in call.args[2]


@pytest.mark.parametrize("compacted", [False, True])
async def test_loading_and_editing_runtime_context_does_not_dirty_the_archive(
    conversation: Conversation, in_memory_engine: AsyncEngine, compacted: bool
) -> None:
    manager = ConversationManager()
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("remember this")]),
            ModelResponse(parts=[TextPart("remembered")]),
        ],
    )
    assert run is not None
    archive_ids = [message.id for message in run.messages]
    context: list[ModelRequest | ModelResponse] = list(run.messages)
    if compacted:
        context = [ModelRequest(parts=[CompactionPart(content="summary")])]
        await manager.record_context_compaction(
            conversation, before=list(run.messages), after=context, trigger="auto"
        )
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert not loaded.runs.loaded
    assert [message.id for message in loaded.messages] == [
        message.id for message in context
    ]
    assert loaded.new_message_index == len(context)
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        async with async_session() as session:
            session.add(loaded)
            assert not session.dirty
            assert not session.new
            loaded.messages = list(loaded.messages)
            loaded.messages.clear()
            loaded.messages.append(ModelRequest(parts=[UserPromptPart("unsaved")]))
            loaded.new_message_index = 0
            assert loaded.revalidate() is loaded
            assert loaded.new_message_index == 0
            assert len(loaded.messages) == 1
            provider = loaded.__transmuter_provided__
            assert provider is not None
            assert not hasattr(provider, "messages")
            assert not hasattr(provider, "new_message_index")
            assert not session.dirty
            assert not session.new
            await session.commit()
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 0
    archive = await manager.get(conversation.id)
    assert archive.messages == []
    assert archive.new_message_index is None
    assert [
        message.id for message in (await ConversationManager().history(archive.id))
    ] == archive_ids
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert [message.id for message in restored.messages] == [
        message.id for message in context
    ]
    assert restored.new_message_index == len(context)
    async with async_session() as session:
        assert await session.count(ModelMessage) == len(archive_ids) + int(compacted)


async def test_context_reuses_rows_and_replaces_cumulative_usage(
    conversation: Conversation, in_memory_engine: AsyncEngine
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    initial = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert initial.new_message_index == 0
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("remember this")]),
            ModelResponse(
                parts=[TextPart("remembered")],
                model_id="provider:model",
                canonical_system="provider",
            ),
        ],
    )
    assert run is not None
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    deferred = DeferredToolRequests(
        approvals=[ToolCallPart("confirm", {}, "approval-1")],
        metadata={"approval-1": {"reason": "external write"}},
    )
    usage = RunUsage(
        requests=3,
        tool_calls=2,
        input_tokens=123,
        output_tokens=45,
        cost=Decimal("0.00123"),
        details={"provider_counter": 7},
        custom_counter=11,
    )
    loaded.usage = usage
    loaded.deferred_tool_requests = deferred
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        assert await store.record_conversation(loaded) == []
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 2
    assert "FROM conversations" in queries.call_args_list[0].args[2]
    assert queries.call_args.args[2].startswith("UPDATE conversations")
    revision = loaded.revision
    for _ in range(2):
        restored = await manager.get(
            conversation.id, with_runs=False, with_messages=True
        )
        assert isinstance(restored, PydanticConversation)
        assert restored.revision == revision
        assert restored.new_message_index == 2
        response = restored.messages[-1]
        assert isinstance(response, ModelResponse)
        assert response.model_id == "provider:model"
        assert response.canonical_system == "provider"
        assert [m.id for m in restored.messages] == [m.id for m in run.messages]
        assert restored.usage == usage
        assert vars(restored.usage)["custom_counter"] == 11
        assert restored.deferred_tool_requests == deferred
        assert await store.record_conversation(restored) == []
        revision = restored.revision
    async with async_session() as session:
        assert await session.count(Conversation) == 1
        assert await session.count(AgentRun) == 1
        assert await session.count(ModelMessage) == 2
        assert await session.count(Compaction) == 0


@pytest.mark.parametrize("registered", [False, True])
async def test_upstream_runs_save_messages_and_usage_once(
    conversation: Conversation, in_memory_engine: AsyncEngine, registered: bool
) -> None:
    def lookup() -> str:
        return "found it"

    manager = ConversationManager()
    store = ConversationStore(manager)
    agent = Agent(
        TestModel(),
        deps_type=type(None),
        tools=[lookup],
        capabilities=[
            History(),
            *([StepPersistence(store=StepStore())] if registered else []),
        ],
    )
    first = await agent.run(
        "look it up",
        conversation=await manager.get(
            conversation.id, with_runs=False, with_messages=True
        ),
    )
    first_conversation = first.conversation
    rows = await store.record_conversation(first_conversation)
    assert len(rows) == len(first.new_messages())
    assert all(row.run_id == first.run_id for row in rows)
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        assert await store.record_conversation(first_conversation) == []
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 2
    assert "FROM conversations" in queries.call_args_list[0].args[2]
    assert queries.call_args.args[2].startswith("UPDATE conversations")
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert first_conversation.revision == restored.revision
    before = restored.usage
    assert before.tool_calls == 1
    assert restored.new_message_index == len(first.all_messages())
    second = await agent.run("continue", conversation=restored)
    assert second.conversation.revision == restored.revision
    assert second.conversation.new_message_index == len(first.all_messages())
    assert second.conversation.usage.requests > before.requests
    assert len(await store.record_conversation(second.conversation)) == len(
        second.new_messages()
    )
    final = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert final.usage == second.conversation.usage
    assert final.new_message_index == len(final.messages)
    async with async_session() as session:
        assert await session.count(AgentRun) == 2
        assert await session.count(ConversationRun) == 2
        assert await session.count(ModelMessage) == len(first.new_messages()) + len(
            second.new_messages()
        )


async def test_record_conversation_rejects_run_results_before_database_access(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    result = AgentRunResult(output="answer")
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        with pytest.raises(ValidationError, match="dataclass_type"):
            # Rejected call is the input under test: callers must extract conversation.
            await store.record_conversation(result)  # pyright: ignore[reportArgumentType]
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 0


async def test_compaction_and_fork_share_rows_and_lifetime_usage(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("old question")]),
            ModelResponse(parts=[TextPart("old answer")]),
            ModelRequest(parts=[UserPromptPart("recent question")]),
            ModelResponse(parts=[TextPart("recent answer")]),
        ],
    )
    assert run is not None
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    usage = RunUsage(requests=4, tool_calls=3, input_tokens=500)
    loaded.usage = usage
    await store.record_conversation(loaded)
    stale = await manager.get(conversation.id, with_runs=False, with_messages=True)
    summary = ModelRequest(parts=[UserPromptPart("retained summary")])
    after = [summary, *run.messages[-2:]]
    compaction = await manager.record_context_compaction(
        loaded, before=list(run.messages), after=after, trigger="manual"
    )
    assert compaction is not None
    assert loaded.revision == stale.revision
    with pytest.raises(ConversationConflict):
        await store.record_conversation(stale)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert [m.id for m in restored.messages] == [m.id for m in after]
    assert restored.usage == usage
    assert restored.new_message_index == 3
    assert await store.record_conversation(restored) == []
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="inkling")
    await manager.fork(conversation, target)
    forked = await manager.get(target.id, with_runs=False, with_messages=True)
    assert [m.id for m in forked.messages] == [m.id for m in after]
    assert forked.usage == usage
    assert forked.revision != restored.revision
    restored.usage = RunUsage(requests=5, tool_calls=4, input_tokens=600)
    await store.record_conversation(restored)
    forked = await manager.get(target.id, with_runs=False, with_messages=True)
    assert forked.usage == usage
    async with async_session() as session:
        assert await session.count(ModelMessage) == 5
        assert await session.count(Compaction) == 1
        assert await session.count(CompactionMembership) == 7
        assert await session.count(ConversationCompaction) == 2
    archived = await manager.get(conversation.id)
    assert [m.id for m in (await ConversationManager().history(archived.id))] == [
        m.id for m in run.messages
    ]
    with pytest.raises(ValueError, match="saved context"):
        await manager.fork(conversation, target)
    empty = await manager.ensure(await a_thread("cut"), agent_tentacle_id="inkling")
    with pytest.raises(ValueError, match="transcript offset"):
        await manager.fork(conversation, empty, end_offset=42)
    with pytest.raises(ValueError, match="unknown conversation"):
        await manager.get(uuid7(), with_runs=False, with_messages=True)


@pytest.mark.parametrize("initialized", [False, True])
async def test_concurrent_saves_have_one_winner(
    conversation: Conversation, initialized: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    if initialized:
        await store.record_conversation(loaded)
    first = await manager.get(conversation.id, with_runs=False, with_messages=True)
    second = await manager.get(conversation.id, with_runs=False, with_messages=True)
    first.usage = RunUsage(tool_calls=1)
    second.usage = RunUsage(tool_calls=2)
    ready = asyncio.Event()
    waiting = 0
    flush = AsyncSession.flush

    async def concurrent_flush(session: AsyncSession) -> None:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            ready.set()
        await ready.wait()
        await flush(session)

    monkeypatch.setattr(AsyncSession, "flush", concurrent_flush)
    results = await asyncio.gather(
        store.record_conversation(first),
        store.record_conversation(second),
        return_exceptions=True,
    )
    assert sum(isinstance(result, list) for result in results) == 1
    assert sum(isinstance(result, ConversationConflict) for result in results) == 1
    conflict = next(
        result for result in results if isinstance(result, ConversationConflict)
    )
    assert isinstance(conflict.__cause__, StaleDataError)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert restored.usage.tool_calls in (1, 2)


@pytest.mark.parametrize("deleted", [False, True])
async def test_conversation_flush_rejects_stale_objects(
    conversation: Conversation, deleted: bool
) -> None:
    async with async_session() as first, async_session() as second:
        winner = await first.one(
            Conversation,
            expressions=[Conversation["id"] == conversation.id],
            options=[lazyload(Conversation["runs"])],
        )
        stale = await second.one(
            Conversation,
            expressions=[Conversation["id"] == conversation.id],
            options=[lazyload(Conversation["runs"])],
        )
        assert winner.revision is None
        if deleted:
            await first.delete(winner)
        else:
            winner.name = "updated"
        await first.commit()
        stale.name = "stale"
        with pytest.raises(StaleDataError):
            await second.flush()

    async with async_session() as session:
        restored = await session.get(Conversation, conversation.id)
        if deleted:
            assert restored is None
        else:
            assert restored is not None
            assert restored.name == "updated"
            assert restored.version == 2
            assert restored.revision is None


async def test_compaction_flush_rejects_a_competing_save(
    conversation: Conversation, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ConversationManager()
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("original context")])],
    )
    assert run is not None
    summary = ModelRequest(parts=[CompactionPart(content="replacement")])
    effective_messages = manager.effective_messages
    revision = str(uuid7())

    async def competing_save(
        current: Conversation,
    ) -> list[ModelRequest | ModelResponse]:
        messages = await effective_messages(current)
        async with async_session() as session:
            winner = await session.one(
                Conversation,
                expressions=[Conversation["id"] == current.id],
                options=[lazyload(Conversation["runs"])],
            )
            winner.revision = revision
            await session.commit()
        return messages

    monkeypatch.setattr(manager, "effective_messages", competing_save)
    with pytest.raises(StaleDataError):
        await manager.record_context_compaction(
            conversation, before=list(run.messages), after=[summary], trigger="auto"
        )
    async with async_session() as session:
        assert await session.count(Compaction) == 0
        assert await session.count(CompactionMembership) == 0
        assert await session.count(ConversationCompaction) == 0
        assert await session.get(ModelRequest, summary.id) is None
    assert [message.id for message in await effective_messages(conversation)] == [
        message.id for message in run.messages
    ]
    restored = await manager.get(conversation.id, with_runs=False)
    assert restored.revision == revision


async def test_stale_save_rolls_back_messages_and_deleted_owners_are_rejected(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    agent = Agent(TestModel(), deps_type=type(None), capabilities=[History()])
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    stale = await agent.run("stale prompt", conversation=loaded)
    winner = await agent.run("winning prompt", conversation=loaded)
    winning_conversation = winner.conversation
    await store.record_conversation(winning_conversation)
    with pytest.raises(ConversationConflict):
        await store.record_conversation(stale.conversation)
    serialized = ConversationTypeAdapter.validate_json(
        ConversationTypeAdapter.dump_json(winning_conversation)
    )
    async with async_session() as session:
        assert await session.get(AgentRun, stale.run_id) is None
        assert await session.count(ModelMessage) == len(winner.new_messages())
        await session.execute(delete(Conversation))
        await session.commit()
    with pytest.raises(ConversationConflict):
        await store.record_conversation(winning_conversation)
    with pytest.raises(ConversationConflict):
        await store.record_conversation(serialized)
    with pytest.raises(ValueError, match="unknown conversation"):
        await manager.get(conversation.id, with_runs=False, with_messages=True)


async def test_serialized_context_appends_once_and_rejects_unrecorded_rewrites(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    agent = Agent(TestModel(), deps_type=type(None), capabilities=[History()])
    first = await agent.run(
        "remember",
        conversation=await manager.get(
            conversation.id, with_runs=False, with_messages=True
        ),
    )
    await store.record_conversation(first.conversation)
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    serialized = ConversationTypeAdapter.validate_json(
        ConversationTypeAdapter.dump_json(loaded)
    )
    second = await agent.run("continue", conversation=serialized)
    await store.record_conversation(second.conversation)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    edited = ConversationTypeAdapter.validate_json(
        ConversationTypeAdapter.dump_json(restored)
    )
    edited.messages[0] = PydanticModelRequest(parts=[UserPromptPart("rewritten")])
    with pytest.raises(ValueError, match="record context compaction"):
        await store.record_conversation(edited)
    async with async_session() as session:
        assert await session.count(ModelMessage) == len(first.new_messages()) + len(
            second.new_messages()
        )


async def test_run_ownership_failure_rolls_back_carryover(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    other = await manager.ensure(await a_thread("other"), agent_tentacle_id="inkling")
    run_id = str(uuid7())
    await StepStore().register_run(
        RunRecord(run_id=run_id, conversation_id=str(other.id))
    )
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    loaded.messages.append(
        ModelRequest(run_id=run_id, parts=[UserPromptPart("wrong owner")])
    )
    loaded.usage = RunUsage(requests=99)
    with pytest.raises(ValueError, match="run belongs"):
        await store.record_conversation(loaded)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert restored.revision is None
    assert restored.usage.requests == 0
    assert not restored.messages


async def test_incremental_save_preserves_an_existing_runs_messages(
    conversation: Conversation,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    loaded = await manager.get(conversation.id, with_runs=False, with_messages=True)
    run_id = str(uuid7())
    loaded.messages.append(
        ModelRequest(run_id=run_id, parts=[UserPromptPart("question")])
    )
    [request] = await store.record_conversation(loaded)
    loaded.messages.append(ModelResponse(run_id=run_id, parts=[TextPart("answer")]))
    [response] = await store.record_conversation(loaded)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert [message.id for message in restored.messages] == [request.id, response.id]
    async with async_session() as session:
        assert await session.count(AgentRun) == 1
        assert await session.count(ConversationRun) == 1
        assert await session.count(ModelMessage) == 2


@pytest.mark.parametrize("with_runs", [False, True])
async def test_upstream_summary_survives_storage_and_continuation(
    conversation: Conversation,
    with_runs: bool,
) -> None:
    manager = ConversationManager()
    store = ConversationStore(manager)
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("old question")]),
            ModelResponse(parts=[TextPart("old answer")]),
            ModelRequest(parts=[UserPromptPart("recent question")]),
            ModelResponse(parts=[TextPart("recent answer")]),
        ],
    )
    assert run is not None
    model = TestModel(custom_output_text="stored summary")
    strategy = SummarizingCompaction[None](
        model=model, max_messages=3, keep_messages=2, preserve_first_user_message=False
    )
    messages = ModelMessagesTypeAdapter.validate_python([vars(m) for m in run.messages])
    compacted = await strategy.compact(
        messages, RunContext(deps=None, model=model, usage=RunUsage())
    )
    assert len(compacted) == 3
    assert isinstance(compacted[0].parts[0], CompactionPart)
    summary = ModelRequest.model_validate(vars(compacted[0]))
    assert compacted[1:] == messages[-2:]
    loaded = await manager.get(conversation.id, with_runs=with_runs)
    await manager.record_context_compaction(
        loaded,
        before=list(run.messages),
        after=[summary, *run.messages[-2:]],
        trigger="manual",
    )
    with pytest.raises(ConversationConflict):
        await store.record_conversation(loaded)
    restored = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert await store.record_conversation(restored) == []
    assert restored.messages[0].id == summary.id
    assert restored.messages[0].parts == compacted[0].parts
    agent = Agent(TestModel(), deps_type=type(None), capabilities=[History()])
    result = await agent.run("continue", conversation=restored)
    await store.record_conversation(result.conversation)
    continued = await manager.get(conversation.id, with_runs=False, with_messages=True)
    assert continued.messages[0].id == summary.id
    assert len(continued.messages) == 3 + len(result.new_messages())
    async with async_session() as session:
        assert await session.count(ModelMessage) == 5 + len(result.new_messages())
        assert await session.count(Compaction) == 1
