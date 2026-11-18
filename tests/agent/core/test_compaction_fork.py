"""Native forks share completed compaction provenance at their history boundary."""

from datetime import UTC, datetime

import pytest
from arcanus import RelationCollection
from pydantic_ai.messages import UserPromptPart
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.schemas.compactions import Compaction, ExternalCompaction
from octomate.schemas.conversation import Conversation, ConversationCompaction
from octomate.schemas.messages import ModelMessage, ModelRequest
from tests.support.managers import a_thread


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek"])
@pytest.mark.parametrize("carry_session", [False, True])
async def test_native_forks_share_compactions_without_reviving_unknown_context(
    agent: str,
    carry_session: bool,
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id=agent)
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id=agent)
    await manager.set_external_id(source, "source-session")
    child = await manager.ensure(
        source.thread_id,
        agent_tentacle_id=agent,
        subagent_id="child",
        parent_conversation_id=source.id,
    )
    summary = ModelRequest(parts=[UserPromptPart("summary")])
    original = Compaction(
        conversation_id=source.id,
        native_session_id="source-session",
        messages=RelationCollection([summary]),
    )
    child_compaction = Compaction(conversation_id=child.id)
    async with async_session() as session:
        session.add_all([original, child_compaction])
        await session.commit()

    assert (
        await manager.fork(
            source,
            target,
            carry_external_id=carry_session,
            external_id=None if carry_session else "fork-session",
        )
        is None
    )
    later = Compaction(conversation_id=source.id)
    async with async_session() as session:
        session.add(later)
        await session.commit()

    descendant = await manager.ensure(
        await a_thread("descendant"), agent_tentacle_id=agent
    )
    await manager.fork(target, descendant, external_id="descendant-session")
    async with async_session() as session:
        links = await session.list(ConversationCompaction)
        assert {(link.conversation_id, link.compaction_id) for link in links} == {
            (target.id, original.id),
            (descendant.id, original.id),
        }
        assert all(link.position is None for link in links)
        assert await session.count(Compaction) == 3
        assert await session.count(ModelMessage) == 1
        saved = await session.get(ModelRequest, summary.id)
        assert saved is not None
        assert saved.compaction_id == original.id
        assert saved.run_id is None
    for conversation in (target, descendant):
        with pytest.raises(ValueError, match="complete, ordered compactions"):
            await manager.get(conversation.id, with_runs=False, with_messages=True)
    async with async_session() as session:
        original_owner = await session.get(Conversation, source.id)
        assert original_owner is not None
        await session.delete(original_owner)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.parametrize("cut", [100, 200])
async def test_transcript_fork_keeps_only_compactions_within_its_session_cut(
    cut: int,
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    ancestor = await manager.ensure(
        await a_thread("ancestor"), agent_tentacle_id="codex"
    )
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="codex")
    await manager.set_external_id(source, "current-session")
    earlier = Compaction(
        conversation_id=ancestor.id, native_session_id="earlier-session"
    )
    observed = [
        ExternalCompaction(
            conversation_id=source.id,
            native_session_id="current-session",
            source="rollout.jsonl",
            start_offset=end - 10,
            end_offset=end,
        )
        for end in (100, 200, 201)
    ]
    async with async_session() as session:
        session.add_all([earlier, *observed])
        await session.flush()
        session.add_all(
            [
                ConversationCompaction(
                    conversation_id=source.id, compaction_id=earlier.id
                ),
                ConversationCompaction(
                    conversation_id=source.id, compaction_id=observed[0].id
                ),
            ]
        )
        await session.commit()

    await manager.fork(source, target, external_id="fork-session", end_offset=cut)
    async with async_session() as session:
        inherited = await session.list(
            ConversationCompaction,
            expressions=[ConversationCompaction["conversation_id"] == target.id],
        )
    assert {link.compaction_id for link in inherited} == {
        earlier.id,
        *(
            row.id
            for row in observed
            if row.end_offset is not None and row.end_offset <= cut
        ),
    }


@pytest.mark.parametrize("native_session_id", [None, "current-session"])
@pytest.mark.parametrize("source_session_id", [None, "current-session"])
async def test_transcript_fork_refuses_an_unknown_compaction_boundary(
    native_session_id: str | None,
    source_session_id: str | None,
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="claude")
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="claude")
    if source_session_id is not None:
        await manager.set_external_id(source, source_session_id)
    async with async_session() as session:
        session.add(
            Compaction(conversation_id=source.id, native_session_id=native_session_id)
        )
        await session.commit()

    with pytest.raises(ValueError, match=r"compaction.*transcript boundary"):
        await manager.fork(source, target, external_id="fork-session", end_offset=100)
    restored = await manager.get(target.id)
    assert restored.external_id is None
    assert not restored.runs
    async with async_session() as session:
        assert await session.count(ConversationCompaction) == 0


@pytest.mark.parametrize("inherited", [False, True])
async def test_fork_refuses_a_target_with_only_compaction_history(
    inherited: bool,
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="codex")
    existing = Compaction(conversation_id=source.id if inherited else target.id)
    async with async_session() as session:
        session.add(existing)
        await session.flush()
        if inherited:
            session.add(
                ConversationCompaction(
                    conversation_id=target.id, compaction_id=existing.id
                )
            )
        await session.commit()
    with pytest.raises(ValueError, match="fork target already holds history"):
        await manager.fork(source, target)


async def test_late_native_correlation_preserves_fork_references() -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    target = await manager.ensure(await a_thread("fork"), agent_tentacle_id="codex")
    timestamp = datetime(2026, 11, 13, tzinfo=UTC)
    original = await manager.record_driven_compaction(
        source,
        native_session_id="session",
        native_compaction_id="item",
        trigger="manual",
        happened_at=timestamp,
    )
    observed = await manager.record_external_compaction(
        source,
        native_session_id="session",
        source="rollout.jsonl",
        start_offset=10,
        end_offset=20,
        happened_at=timestamp,
        messages=[],
    )
    assert original is not None
    assert observed is not None
    async with async_session() as session:
        session.add_all(
            [
                ConversationCompaction(
                    conversation_id=target.id, compaction_id=original.id
                ),
                ConversationCompaction(
                    conversation_id=target.id, compaction_id=observed.id
                ),
                ConversationCompaction(
                    conversation_id=source.id, compaction_id=observed.id
                ),
            ]
        )
        await session.commit()
    await manager.record_external_compaction(
        source,
        native_session_id="session",
        native_compaction_id="item",
        source="rollout.jsonl",
        start_offset=10,
        end_offset=20,
        happened_at=timestamp,
        messages=[],
    )
    async with async_session() as session:
        assert await session.count(Compaction) == 1
        links = await session.list(ConversationCompaction)
        assert {(link.conversation_id, link.compaction_id) for link in links} == {
            (source.id, original.id),
            (target.id, original.id),
        }
