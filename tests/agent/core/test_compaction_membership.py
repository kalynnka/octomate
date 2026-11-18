"""Compaction context references preserve order, ownership, and shared history."""

import pytest
from arcanus import RelationCollection
from pydantic import UUID7, ValidationError
from pydantic_ai.messages import TextPart, UserPromptPart
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.schemas.compactions import Compaction, CompactionMembership
from octomate.schemas.conversation import Conversation, ConversationCompaction
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from octomate.schemas.runs import AgentRun
from octomate.types.compactions import CompactionStage
from tests.support.managers import a_thread

type History = tuple[Conversation, AgentRun, Compaction]


@pytest.fixture
async def history(in_memory_engine: AsyncEngine) -> History:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="agent")
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("Original prompt")]),
            ModelResponse(parts=[TextPart("Replaced answer")]),
            ModelRequest(parts=[UserPromptPart("Retained follow-up")]),
        ],
    )
    assert run is not None
    compaction = Compaction(
        conversation_id=conversation.id,
        messages=RelationCollection([ModelRequest(parts=[UserPromptPart("Summary")])]),
    )
    async with async_session() as session:
        session.add(compaction)
        await session.commit()
    return conversation, run, compaction


async def test_ordered_membership_reuses_retained_messages_without_changing_owners(
    history: History,
) -> None:
    conversation, run, compaction = history
    before = [message.id for message in run.messages]
    after = [compaction.messages[0].id, before[2], before[0]]
    stages: tuple[tuple[CompactionStage, list[UUID7]], ...] = (
        ("before", before),
        ("after", after),
    )
    rows = [
        CompactionMembership(
            compaction_id=compaction.id,
            stage=stage,
            position=position,
            model_message_id=message_id,
        )
        for stage, ids in stages
        for position, message_id in enumerate(ids)
    ]
    async with async_session() as session:
        session.add_all(reversed(rows))
        stored = await session.get(Compaction, compaction.id)
        assert stored is not None
        assert not stored.membership_complete
        stored.membership_complete = True
        await session.commit()
    async with async_session() as session:
        for stage, expected in (("before", before), ("after", after)):
            memberships = await session.list(
                CompactionMembership,
                expressions=[
                    CompactionMembership["compaction_id"] == compaction.id,
                    CompactionMembership["stage"] == stage,
                ],
                order_bys=[CompactionMembership["position"]],
            )
            assert [row.model_message_id for row in memberships] == expected
            for row in memberships:
                assert (
                    CompactionMembership.model_validate_json(
                        row.model_dump_json()
                    ).model_dump()
                    == row.model_dump()
                )
        assert await session.count(ModelMessage) == 4
        stored = await session.get(Compaction, compaction.id)
        assert stored is not None
        assert stored.membership_complete
        assert not stored.messages.loaded
        retained = await session.get(ModelRequest, before[2])
        assert retained is not None
        assert retained.run_id == run.id
        assert retained.compaction_id is None
    archive = await ConversationManager().get(conversation.id)
    assert [
        message.id for message in (await ConversationManager().history(archive.id))
    ] == before


@pytest.mark.parametrize("duplicate", ["position", "message"])
async def test_membership_has_one_message_at_each_position(
    history: History, duplicate: str
) -> None:
    _, run, compaction = history
    async with async_session() as session:
        session.add(
            CompactionMembership(
                compaction_id=compaction.id,
                stage="before",
                position=0,
                model_message_id=run.messages[0].id,
            )
        )
        await session.commit()
    async with async_session() as session:
        session.add(
            CompactionMembership(
                compaction_id=compaction.id,
                stage="before",
                position=0 if duplicate == "position" else 1,
                model_message_id=run.messages[1 if duplicate == "position" else 0].id,
            )
        )
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            await session.commit()


@pytest.mark.parametrize("invalid", [{"stage": "during"}, {"position": -1}])
async def test_membership_rejects_invalid_stage_and_position_at_both_boundaries(
    history: History, invalid: dict[str, str | int]
) -> None:
    _, run, compaction = history
    payload = {
        "compaction_id": compaction.id,
        "stage": "before",
        "position": 0,
        "model_message_id": run.messages[0].id,
    }
    payload.update(invalid)
    with pytest.raises(ValidationError):
        CompactionMembership.model_validate(payload)
    async with async_session() as session:
        session.add(CompactionMembership.model_construct(_fields_set=None, **payload))
        with pytest.raises(IntegrityError, match="CHECK constraint failed"):
            await session.commit()


@pytest.mark.parametrize("missing", ["compaction", "message"])
async def test_membership_requires_existing_archive_parents(
    history: History, missing: str
) -> None:
    _, run, compaction = history
    async with async_session() as session:
        session.add(
            CompactionMembership(
                compaction_id=uuid7() if missing == "compaction" else compaction.id,
                stage="before",
                position=0,
                model_message_id=uuid7()
                if missing == "message"
                else run.messages[0].id,
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


async def test_referenced_archive_messages_cannot_be_deleted(history: History) -> None:
    _, run, compaction = history
    async with async_session() as session:
        session.add(
            CompactionMembership(
                compaction_id=compaction.id,
                stage="before",
                position=0,
                model_message_id=run.messages[0].id,
            )
        )
        await session.commit()
    async with async_session() as session:
        original = await session.get(ModelRequest, run.messages[0].id)
        assert original is not None
        await session.delete(original)
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


async def test_shared_compaction_history_freezes_links_and_protects_the_source(
    history: History,
) -> None:
    source, _, compaction = history
    manager = ConversationManager()
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="agent")
    later = Compaction(conversation_id=source.id)
    async with async_session() as session:
        session.add(later)
        await session.flush()
        session.add_all(
            [
                ConversationCompaction(
                    conversation_id=source.id, compaction_id=compaction.id, position=0
                ),
                ConversationCompaction(
                    conversation_id=target.id, compaction_id=compaction.id, position=0
                ),
                ConversationCompaction(
                    conversation_id=source.id, compaction_id=later.id, position=1
                ),
            ]
        )
        await session.commit()
    async with async_session() as session:
        inherited = await session.list(
            ConversationCompaction,
            expressions=[ConversationCompaction["conversation_id"] == target.id],
        )
        assert [row.compaction_id for row in inherited] == [compaction.id]
        assert inherited[0].position == 0
        assert (
            ConversationCompaction.model_validate_json(
                inherited[0].model_dump_json()
            ).model_dump()
            == inherited[0].model_dump()
        )
        original = await session.get(Conversation, source.id)
        assert original is not None
        await session.delete(original)
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()
    async with async_session() as session:
        fork = await session.get(Conversation, target.id)
        assert fork is not None
        await session.delete(fork)
        await session.commit()
        assert await session.count(Compaction) == 2
        original = await session.get(Conversation, source.id)
        assert original is not None
        await session.delete(original)
        await session.commit()
        assert await session.count(Compaction) == 0


async def test_unknown_compaction_order_is_distinct_from_conflicting_known_positions(
    history: History,
) -> None:
    source, _, first = history
    second = Compaction(conversation_id=source.id)
    async with async_session() as session:
        session.add(second)
        await session.flush()
        session.add_all(
            ConversationCompaction(conversation_id=source.id, compaction_id=row.id)
            for row in (first, second)
        )
        await session.commit()
    async with async_session() as session:
        rows = await session.list(ConversationCompaction)
        assert len(rows) == 2
        assert all(row.position is None for row in rows)
        for row in rows:
            row.position = 0
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            await session.commit()
