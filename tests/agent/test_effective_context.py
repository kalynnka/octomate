"""Complete compactions replace effective prefixes while keeping the archive intact."""

from unittest.mock import Mock

import pytest
from arcanus import RelationCollection
from pydantic_ai.messages import SystemPromptPart, TextPart, UserPromptPart
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.models.messages import ModelMessage as ModelMessageModel
from octomate.schemas.compactions import Compaction, CompactionMembership
from octomate.schemas.conversation import ConversationCompaction
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from tests.support.managers import a_thread


async def test_context_read_query_count_does_not_grow_with_compactions(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    query_counts: list[int] = []
    for count in (1, 5):
        conversation = await manager.ensure(
            await a_thread(f"history-{count}"), agent_tentacle_id="inkling"
        )
        run = await manager.record_agent_run(
            conversation,
            str(uuid7()),
            [ModelRequest(parts=[UserPromptPart("original")])],
        )
        assert run is not None
        effective = list(run.messages)
        for index in range(count):
            after: list[ModelRequest | ModelResponse] = [
                ModelRequest(parts=[SystemPromptPart(f"summary {index}")])
            ]
            revision = await manager.record_context_compaction(
                conversation, before=effective, after=after, trigger="auto"
            )
            assert revision is not None
            conversation.revision = revision
            effective = after

        queries = Mock()
        loaded_messages = Mock()
        event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
        event.listen(ModelMessageModel, "load", loaded_messages, propagate=True)
        try:
            restored = await manager.effective_messages(conversation)
        finally:
            event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
            event.remove(ModelMessageModel, "load", loaded_messages)
        assert [message.id for message in restored] == [
            message.id for message in effective
        ]
        assert loaded_messages.call_count == len(effective)
        query_counts.append(queries.call_count)

    assert query_counts == [6, 6]


async def test_rewrites_preserve_archive_owners_and_append_later_messages(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("original")]),
            ModelResponse(parts=[TextPart("old answer")]),
            ModelRequest(parts=[UserPromptPart("recent")]),
            ModelResponse(parts=[TextPart("answer after compaction")]),
        ],
    )
    assert run is not None
    original = list(run.messages)
    summary = ModelRequest(parts=[SystemPromptPart("summary")])
    # The last response was generated after the rewrite within the same run.
    revision = await manager.record_context_compaction(
        conversation,
        before=original[:3],
        after=[summary, original[2]],
        trigger="auto",
    )
    assert revision is not None
    conversation.revision = revision
    effective = await manager.effective_messages(conversation)
    assert [message.id for message in effective] == [
        summary.id,
        original[2].id,
        original[3].id,
    ]
    archived = await manager.get(conversation.id)
    assert [
        message.id for message in (await ConversationManager().history(archived.id))
    ] == [message.id for message in original]
    async with async_session() as session:
        compacted = await session.one(Compaction)
        assert compacted.membership_complete
        assert compacted.native_compaction_id is None
        stored = await session.get(ModelRequest, original[2].id)
        assert stored is not None
        assert stored.run_id == run.id
        assert stored.compaction_id is None
        replacement = await session.get(ModelRequest, summary.id)
        assert replacement is not None
        assert replacement.run_id is None
        assert replacement.compaction_id == compacted.id
        assert replacement.conversation_id == str(conversation.id)
        assert await session.count(CompactionMembership) == 5

    later = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("next")])],
    )
    assert later is not None
    next_summary = ModelRequest(parts=[SystemPromptPart("updated summary")])
    revision = await manager.record_context_compaction(
        conversation,
        before=effective,
        after=[effective[1], next_summary, effective[2]],
        trigger="auto",
    )
    assert revision is not None
    assert [
        message.id for message in await manager.effective_messages(conversation)
    ] == [
        original[2].id,
        next_summary.id,
        original[3].id,
        later.messages[0].id,
    ]
    async with async_session() as session:
        assert await session.count(ModelMessage) == 7
        links = await session.list(
            ConversationCompaction,
            order_bys=[ConversationCompaction["position"]],
        )
        assert [(link.compaction_id, link.position) for link in links] == [
            (compacted.id, 0),
            (next_summary.compaction_id, 1),
        ]


async def test_noop_and_invalid_prefix_do_not_record_compactions(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    run = await manager.record_agent_run(
        conversation,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("first")]),
            ModelResponse(parts=[TextPart("second")]),
        ],
    )
    assert run is not None
    before = list(run.messages)
    assert (
        await manager.record_context_compaction(
            conversation, before=before, after=before, trigger="manual"
        )
        is None
    )
    with pytest.raises(ValueError, match="does not match"):
        await manager.record_context_compaction(
            conversation, before=before[1:], after=[], trigger="manual"
        )
    with pytest.raises(ValueError, match="unique"):
        await manager.record_context_compaction(
            conversation, before=before, after=[before[0], before[0]], trigger="auto"
        )
    async with async_session() as session:
        assert await session.count(Compaction) == 0

    compacted = await manager.record_context_compaction(
        conversation, before=before, after=[], trigger="manual"
    )
    assert compacted is not None
    assert await manager.effective_messages(conversation) == []
    assert (
        len(
            await ConversationManager().history((await manager.get(conversation.id)).id)
        )
        == 2
    )


@pytest.mark.parametrize(
    ("position", "complete"), [(None, True), (1, True), (0, False)]
)
async def test_unknown_context_is_refused_instead_of_replaying_the_archive(
    in_memory_engine: AsyncEngine, position: int | None, complete: bool
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    compacted = Compaction(
        conversation_id=conversation.id,
        membership_complete=complete,
        messages=RelationCollection(),
    )
    async with async_session() as session:
        session.add(compacted)
        await session.flush()
        session.add(
            ConversationCompaction(
                conversation_id=conversation.id,
                compaction_id=compacted.id,
                position=position,
            )
        )
        await session.commit()
    with pytest.raises(ValueError, match="complete, ordered"):
        await manager.effective_messages(conversation)


async def test_unlinked_compaction_does_not_restore_the_archive(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    async with async_session() as session:
        session.add(Compaction(conversation_id=conversation.id))
        await session.commit()
    with pytest.raises(ValueError, match="every compaction to be linked"):
        await manager.effective_messages(conversation)
