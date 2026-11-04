"""Completed transformation identities round-trip through the shared persistence layer."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.schemas.compactions import (
    Compaction,
    CompactionAdapter,
    ExternalCompaction,
)
from octomate.types.compactions import CompactionTrigger
from tests.support.managers import a_thread


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
        native_id="codex",
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
    assert by_id[owned.id].native_id is None
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
    assert stored.native_id is None
    assert stored.native_session_id is None
    assert stored.native_compaction_id is None
    assert stored.source is None
    assert stored.start_offset is None
    assert stored.end_offset is None


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
