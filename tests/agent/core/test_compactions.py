"""Completed transformation identities round-trip through the shared persistence layer."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from arcanus import RelationCollection
from arcanus.materia.sqlalchemy import selectinload
from pydantic import ValidationError
from pydantic_ai.messages import (
    CompactionPart,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.schemas.compactions import (
    Compaction,
    CompactionAdapter,
    ExternalCompaction,
)
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from octomate.types.compactions import CompactionTrigger
from tests.support.managers import a_thread


async def test_native_and_driven_observations_share_a_lock_across_conversations(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    native = await manager.ensure(await a_thread("native"), agent_tentacle_id="codex")
    driven = await manager.ensure(await a_thread("driven"), agent_tentacle_id="codex")
    await manager.set_external_id(driven, "session")
    await asyncio.gather(
        *(
            manager.record_driven_compaction(
                driven,
                native_session_id="session",
                native_compaction_id="compaction",
                trigger="manual",
                happened_at=None,
            )
            if index % 2
            else manager.record_external_compaction(
                native,
                native_session_id="session",
                native_compaction_id="compaction",
                source="rollout.jsonl",
                start_offset=10,
                end_offset=20,
                happened_at=datetime(2026, 11, 6, tzinfo=UTC),
                messages=[ModelRequest(parts=[UserPromptPart("Summary")])],
            )
            for index in range(10)
        )
    )
    async with async_session() as session:
        [stored] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert stored.conversation_id == driven.id
    assert stored.trigger == "manual"
    assert [message.message_text for message in stored.messages] == ["Summary"]


async def test_same_session_and_item_stay_distinct_for_different_agents(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    owners = set()
    for agent_id in ("codex", "another-codex"):
        driven = await manager.ensure(
            await a_thread(f"driven-{agent_id}"), agent_tentacle_id=agent_id
        )
        native = await manager.ensure(
            await a_thread(f"native-{agent_id}"), agent_tentacle_id=agent_id
        )
        owners.add(driven.id)
        await manager.set_external_id(driven, "shared-session")
        await manager.record_external_compaction(
            native,
            native_session_id="shared-session",
            native_compaction_id="shared-item",
            source="rollout.jsonl",
            start_offset=10,
            end_offset=20,
            happened_at=datetime(2026, 11, 6, tzinfo=UTC),
            messages=[],
        )
        await manager.record_driven_compaction(
            driven,
            native_session_id="shared-session",
            native_compaction_id="shared-item",
            trigger="manual",
            happened_at=None,
        )
    async with async_session() as session:
        rows = await session.list(Compaction)
    assert len(rows) == 2
    assert {row.conversation_id for row in rows} == owners
    assert all(row.trigger == "manual" for row in rows)


async def test_distinct_compaction_ids_without_byte_offsets_keep_separate_records(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="deepseek")
    for native_compaction_id in ("first", "second", "first", "second"):
        await manager.record_external_compaction(
            conversation,
            native_session_id="session",
            native_compaction_id=native_compaction_id,
            source="history.jsonl",
            start_offset=None,
            end_offset=None,
            happened_at=datetime(2026, 11, 6, tzinfo=UTC),
            messages=[],
        )
    async with async_session() as session:
        rows = await session.list(Compaction)
    assert {row.native_compaction_id for row in rows} == {"first", "second"}
    assert len(rows) == 2


async def test_driven_compaction_accepts_later_content_without_replacing_identity(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="agent")
    original = await manager.record_driven_compaction(
        conversation,
        native_session_id="session",
        native_compaction_id="compact",
        trigger="manual",
        happened_at=None,
    )
    assert original is not None
    message = ModelRequest(parts=[UserPromptPart("Recovered summary")])
    for _ in range(2):
        assert (
            await manager.record_driven_compaction(
                conversation,
                native_session_id="session",
                native_compaction_id="compact",
                trigger="manual",
                happened_at=None,
                messages=[message],
            )
            is None
        )
    async with async_session() as session:
        [stored] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert stored.id == original.id
    assert [item.id for item in stored.messages] == [message.id]
    assert stored.messages[0].run_id is None
    assert not (await manager.get(conversation.id)).runs


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("count", [2, 100])
async def test_compaction_metadata_enrichment_is_persisted_in_one_batch(
    in_memory_engine: AsyncEngine, external: bool, count: int
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="agent")
    originals: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[UserPromptPart(str(index))], metadata={"source": "original"}
        )
        if index % 2
        else ModelResponse(
            parts=[TextPart(str(index))], metadata={"source": "original"}
        )
        for index in range(count)
    ]
    compaction_type = ExternalCompaction if external else Compaction
    compaction = compaction_type(
        conversation_id=conversation.id,
        native_session_id="session",
        native_compaction_id="compact",
        trigger="manual",
        messages=RelationCollection(originals),
    )
    async with async_session() as session:
        session.add(compaction)
        await session.commit()
    observed = [
        ModelRequest(parts=message.parts, metadata={"native_item_ids": [str(index)]})
        if isinstance(message, ModelRequest)
        else ModelResponse(
            parts=message.parts, metadata={"native_item_ids": [str(index)]}
        )
        for index, message in enumerate(originals)
    ]
    queries = Mock()
    for expected_updates in (1, 0):
        queries.reset_mock()
        event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
        try:
            if external:
                await manager.record_external_compaction(
                    conversation,
                    native_session_id="session",
                    native_compaction_id="compact",
                    source="transcript.jsonl",
                    start_offset=10,
                    end_offset=20,
                    happened_at=datetime(2026, 11, 6, tzinfo=UTC),
                    messages=observed,
                )
            else:
                await manager.record_driven_compaction(
                    conversation,
                    native_session_id="session",
                    native_compaction_id="compact",
                    trigger="manual",
                    happened_at=None,
                    messages=observed,
                )
        finally:
            event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
        assert (
            sum(
                call.args[2].startswith("UPDATE model_messages")
                for call in queries.call_args_list
            )
            == expected_updates
        )
    async with async_session() as session:
        stored = await session.get(
            Compaction, compaction.id, options=[selectinload(Compaction["messages"])]
        )
        assert await session.count(ModelMessage) == count
    assert type(stored) is compaction_type
    assert stored is not None
    assert [message.id for message in stored.messages] == [m.id for m in originals]
    for index, message in enumerate(stored.messages):
        assert message.parts == originals[index].parts
        assert message.metadata == {
            "source": "original",
            "native_item_ids": [str(index)],
        }
        assert message.run_id is None
        assert message.compaction_id == compaction.id


async def test_driven_compactions_are_deduplicated_by_native_session_and_item(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="codex")
    results = await asyncio.gather(
        *(
            manager.record_driven_compaction(
                conversation,
                native_session_id=session_id,
                native_compaction_id="same-item",
                trigger="manual",
                happened_at=None,
            )
            for session_id in ("first-session", "first-session", "second-session")
        )
    )
    assert sum(result is not None for result in results) == 2
    async with async_session() as session:
        rows = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert len(rows) == 2
    assert {row.native_session_id for row in rows} == {
        "first-session",
        "second-session",
    }
    assert all(type(row) is Compaction for row in rows)
    assert all(row.happened_at is None and not row.messages for row in rows)
    assert not (await manager.get(conversation.id)).runs


@pytest.mark.parametrize("trigger", ["manual", "auto", "unknown"])
async def test_compaction_variants_persist_without_model_runs(
    in_memory_engine: AsyncEngine, trigger: CompactionTrigger
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="agent")
    instant = datetime(2026, 11, 4, 10, tzinfo=timezone(timedelta(hours=8)))
    owned = Compaction(conversation_id=conversation.id, trigger=trigger)
    driven = Compaction(
        conversation_id=conversation.id,
        trigger=trigger,
        native_session_id="native-session",
        native_compaction_id="native-compaction",
        happened_at=instant,
    )
    external = ExternalCompaction(
        conversation_id=conversation.id,
        trigger=trigger,
        source="transcript.jsonl",
        start_offset=0,
        end_offset=128,
        happened_at=instant,
    )
    async with async_session() as session:
        session.add_all([owned, driven, external])
        await session.commit()

    async with async_session() as session:
        rows = await session.list(Compaction)
    by_id = {row.id: row for row in rows}
    assert type(by_id[owned.id]) is Compaction
    assert type(by_id[driven.id]) is Compaction
    assert isinstance(by_id[external.id], ExternalCompaction)
    for original in (owned, driven, external):
        stored = by_id[original.id]
        assert stored.model_dump() == original.model_dump()
        restored = CompactionAdapter.validate_json(stored.model_dump_json())
        assert type(restored) is type(stored)
        assert restored.model_dump() == stored.model_dump()
        assert stored.id.version == 7
        assert stored.recorded_at.tzinfo is UTC
    assert by_id[owned.id].happened_at is None
    assert by_id[driven.id].happened_at == instant.astimezone(UTC)
    assert (await manager.get(conversation.id)).runs == []


async def test_unreported_external_provenance_stays_unknown(
    in_memory_engine: AsyncEngine,
) -> None:
    conversation = await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="agent"
    )
    observation = ExternalCompaction(conversation_id=conversation.id)
    async with async_session() as session:
        session.add(observation)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            Compaction, expressions=[Compaction["id"] == observation.id]
        )
    assert isinstance(stored, ExternalCompaction)
    assert stored.trigger == "unknown"
    assert stored.happened_at is None
    assert stored.native_session_id is None
    assert stored.native_compaction_id is None
    assert stored.source is None
    assert stored.start_offset is None
    assert stored.end_offset is None


@pytest.mark.parametrize("compaction_type", [Compaction, ExternalCompaction])
async def test_compaction_content_is_loaded_only_when_requested(
    in_memory_engine: AsyncEngine, compaction_type: type[Compaction]
) -> None:
    conversation = await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="codex"
    )
    summary = ModelRequest(parts=[SystemPromptPart("summary")])
    compaction = compaction_type(
        conversation_id=conversation.id,
        messages=RelationCollection([summary]),
    )
    async with async_session() as session:
        session.add(compaction)
        await session.commit()

    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        async with async_session() as session:
            metadata = await session.one_or_none(
                Compaction, expressions=[Compaction["id"] == compaction.id]
            )
            assert metadata is not None
            assert metadata.id == compaction.id
            assert not metadata.messages.loaded
            with pytest.raises(InvalidRequestError, match="selectinload"):
                list(metadata.messages)
        assert queries.call_count == 1

        queries.reset_mock()
        async with async_session() as session:
            loaded = await session.one_or_none(
                Compaction,
                expressions=[Compaction["id"] == compaction.id],
                options=[selectinload(Compaction["messages"])],
            )
        assert queries.call_count == 2
        assert loaded is not None
        assert loaded.messages.loaded
        assert [message.id for message in loaded.messages] == [summary.id]
        assert loaded.messages[0].parts == summary.parts
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)


@pytest.mark.parametrize("compaction_type", [Compaction, ExternalCompaction])
@pytest.mark.parametrize("opaque", [False, True])
async def test_compaction_messages_preserve_content_and_the_run_archive(
    in_memory_engine: AsyncEngine,
    compaction_type: type[Compaction],
    opaque: bool,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="codex")
    run = await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[
            ModelRequest(parts=[UserPromptPart("original prompt")]),
            ModelResponse(parts=[TextPart("original answer")]),
        ],
    )
    assert run is not None
    archive = [message.id for message in run.messages]
    summary = ModelRequest(parts=[SystemPromptPart("readable context summary")])
    checkpoint = ModelResponse(
        parts=[
            CompactionPart(
                content=None if opaque else "provider summary",
                id="checkpoint",
                provider_name="openai" if opaque else "anthropic",
                provider_details={"encrypted_content": "opaque-data"}
                if opaque
                else None,
            )
        ],
        metadata={"source": "compaction"},
    )
    compaction = compaction_type(
        conversation_id=conversation.id,
        messages=RelationCollection([summary, checkpoint]),
    )
    async with async_session() as session:
        session.add(compaction)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            Compaction,
            expressions=[Compaction["id"] == compaction.id],
            options=[selectinload(Compaction["messages"])],
        )
        rows = await session.list(
            ModelMessage,
            expressions=[ModelMessage["compaction_id"] == compaction.id],
            order_bys=[ModelMessage["id"]],
            options=[selectinload(ModelMessage["thread_messages"])],
        )
    assert type(stored) is compaction_type
    assert stored is not None
    assert [message.id for message in stored.messages] == [summary.id, checkpoint.id]
    restored = CompactionAdapter.validate_json(stored.model_dump_json())
    assert type(restored) is compaction_type
    assert [message.parts for message in restored.messages] == [
        summary.parts,
        checkpoint.parts,
    ]
    assert [type(message) for message in rows] == [ModelRequest, ModelResponse]
    for message, original in zip(rows, (summary, checkpoint), strict=True):
        assert isinstance(message, ModelRequest | ModelResponse)
        assert message.run_id is None
        assert message.compaction_id == compaction.id
        assert message.parts == original.parts
        assert message.metadata == original.metadata
        assert message.id.version == 7
        assert message.thread_messages == []
    reloaded = await manager.get(conversation.id)
    assert [item.id for item in reloaded.runs] == [run.id]
    assert [
        message.id for message in (await ConversationManager().history(reloaded.id))
    ] == archive
    async with async_session() as session:
        await session.delete(stored)
        await session.commit()
    async with async_session() as session:
        remaining = await session.list(ModelMessage, order_bys=[ModelMessage["id"]])
    assert [message.id for message in remaining] == archive


@pytest.mark.parametrize("owner", ["neither", "both"])
async def test_persisted_messages_require_exactly_one_owner(
    in_memory_engine: AsyncEngine, owner: str
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="codex")
    run = await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[ModelResponse(parts=[TextPart("original answer")])],
    )
    assert run is not None
    compaction = Compaction(conversation_id=conversation.id)
    async with async_session() as session:
        session.add(compaction)
        await session.commit()
    message = ModelRequest(
        parts=[SystemPromptPart("summary")],
        run_id=run.id if owner == "both" else None,
        compaction_id=compaction.id if owner == "both" else None,
    )
    async with async_session() as session:
        session.add(message)
        with pytest.raises(IntegrityError, match="ck_model_messages_single_owner"):
            await session.commit()


async def test_compaction_messages_require_an_existing_compaction(
    in_memory_engine: AsyncEngine,
) -> None:
    async with async_session() as session:
        session.add(
            ModelRequest(parts=[SystemPromptPart("summary")], compaction_id=uuid7())
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


async def test_compaction_requires_an_existing_conversation(
    in_memory_engine: AsyncEngine,
) -> None:
    async with async_session() as session:
        session.add(Compaction(conversation_id=uuid7()))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


@pytest.mark.parametrize(("start", "end"), [(-1, None), (None, -1), (128, 64)])
def test_external_compaction_rejects_invalid_transcript_ranges(
    start: int | None, end: int | None
) -> None:
    with pytest.raises(
        ValidationError, match=r"greater than or equal|must not precede"
    ):
        ExternalCompaction(conversation_id=uuid7(), start_offset=start, end_offset=end)


@pytest.mark.parametrize("kind", ["octomate", "external"])
@pytest.mark.parametrize(
    "invalid", [{"trigger": "failed"}, {"happened_at": "2026-11-04T10:00:00"}]
)
def test_compaction_boundary_rejects_unknown_triggers_and_naive_source_times(
    kind: str, invalid: dict[str, str]
) -> None:
    with pytest.raises(ValidationError, match=r"trigger|happened_at"):
        CompactionAdapter.validate_python(
            {"kind": kind, "conversation_id": uuid7(), **invalid}
        )


def test_compaction_boundary_requires_a_known_variant() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CompactionAdapter.validate_python(
            {"kind": "claude", "conversation_id": uuid7()}
        )
