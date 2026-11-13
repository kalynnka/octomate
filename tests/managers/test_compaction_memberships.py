"""Native membership enriches existing records without changing archive ownership."""

from unittest.mock import Mock

import pytest
from arcanus import RelationCollection
from pydantic_ai.messages import UserPromptPart
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.schemas.compactions import (
    Compaction,
    CompactionMembership,
    NativeContext,
)
from octomate.schemas.messages import ModelRequest
from octomate.types.compactions import CompactionRewrite
from tests.support.managers import a_thread


@pytest.mark.parametrize("split", [False, True])
async def test_native_aliases_preserve_message_identity_and_require_whole_messages(
    in_memory_engine: AsyncEngine, split: bool
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    messages = [
        ModelRequest(
            parts=[UserPromptPart("same prompt")],
            metadata={"native_item_ids": [identity]},
        )
        for identity in ("driven-a", "driven-b")
    ]
    if split:
        messages[0].parts = [*messages[0].parts, *messages.pop().parts]
        messages[0].metadata = {"native_item_ids": ["driven-a", "driven-b"]}
    run = await manager.record_agent_run(
        source, str(uuid7()), messages, external_id="native"
    )
    assert run is not None
    archive = {
        message.id: (
            message.run_id,
            message.compaction_id,
            message.parts,
            message.metadata,
        )
        for message in run.messages
    }
    compaction = await manager.record_driven_compaction(
        source,
        native_session_id="native",
        native_compaction_id="compaction",
        trigger="manual",
        happened_at=None,
        messages=[
            ModelRequest(
                parts=[UserPromptPart("summary")],
                metadata={
                    "native_item_ids": ["summary"],
                    "compaction_context": NativeContext(
                        last_item_id="transcript-b",
                        replacement_ids=["summary", "transcript-b"],
                        item_aliases={
                            "driven-a": "transcript-a",
                            "driven-b": "transcript-b",
                        },
                    ).model_dump(mode="json"),
                },
            )
        ],
    )
    assert compaction is not None
    child = await manager.ensure(await a_thread("child"), agent_tentacle_id="codex")
    await manager.fork(source, child, external_id="child-session")
    for _ in range(2):
        await manager.record_native_memberships(source, "native")
        async with async_session() as session:
            stored = await session.get(Compaction, compaction.id)
            assert stored is not None
            assert stored.membership_complete is not split
            assert await session.count(CompactionMembership) == (0 if split else 4)
        assert {
            message.id: (
                message.run_id,
                message.compaction_id,
                message.parts,
                message.metadata,
            )
            for message in await manager.history(source.id)
        } == archive
        if not split:
            expected = [compaction.messages[0].id, run.messages[-1].id]
            assert [
                message.id for message in await manager.effective_messages(source)
            ] == expected
            assert [
                message.id for message in await manager.effective_messages(child)
            ] == expected


@pytest.mark.parametrize("count", [1, 3])
async def test_memberships_are_batched_immutable_and_fill_inherited_order(
    in_memory_engine: AsyncEngine,
    count: int,
) -> None:
    manager = ConversationManager()
    source = await manager.ensure(
        await a_thread("source"), agent_tentacle_id="deepseek"
    )
    await manager.record_external_run(
        source,
        run_id="native:1",
        native_session_id="native",
        end_offset=4,
        messages=[
            ModelRequest(parts=[UserPromptPart(text)]) for text in ("replace", "retain")
        ],
    )
    archive = await manager.history(source.id)
    compactions = [
        Compaction(
            conversation_id=source.id,
            native_session_id="native",
            messages=RelationCollection(
                [ModelRequest(parts=[UserPromptPart(f"summary {index}")])]
            ),
        )
        for index in range(count)
    ]
    async with async_session() as session:
        session.add_all(compactions)
        await session.commit()
    fork = await manager.ensure(await a_thread("fork"), agent_tentacle_id="deepseek")
    await manager.fork(source, fork, external_id="child")
    before = [message.id for message in archive]
    rewrites: list[CompactionRewrite] = []
    for compaction in compactions:
        after = [compaction.messages[0].id, archive[-1].id]
        rewrites.append(CompactionRewrite(compaction.id, before, after))
        before = after
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        await manager.record_compaction_memberships(source, rewrites)
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert (
        sum(call.args[2].startswith("SELECT") for call in queries.call_args_list) == 5
    )
    assert [
        message.id for message in await manager.effective_messages(source)
    ] == before
    assert [message.id for message in await manager.effective_messages(fork)] == before
    assert [message.id for message in await manager.history(source.id)] == [
        message.id for message in archive
    ]
    queries.reset_mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        await manager.record_compaction_memberships(source, rewrites)
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert all(call.args[2].startswith("SELECT") for call in queries.call_args_list)
    changed = [
        rewrites[0]._replace(after=list(reversed(rewrites[0].after))),
        *rewrites[1:],
    ]
    with pytest.raises(ValueError, match="immutable"):
        await manager.record_compaction_memberships(source, changed)
    async with async_session() as session:
        assert await session.count(CompactionMembership) == count * 4
        saved = await session.get(ModelRequest, archive[-1].id)
        assert saved is not None
        assert saved.run_id == "native:1"
        assert saved.compaction_id is None


@pytest.mark.parametrize("invalid", ["input", "replacement", "duplicate"])
async def test_invalid_membership_does_not_complete_the_compaction(
    in_memory_engine: AsyncEngine,
    invalid: str,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="deepseek")
    await manager.record_external_run(
        conversation,
        run_id="native:1",
        native_session_id="native",
        end_offset=4,
        messages=[ModelRequest(parts=[UserPromptPart("original")])],
    )
    [original] = await manager.history(conversation.id)
    summary = ModelRequest(parts=[UserPromptPart("summary")])
    compaction = Compaction(
        conversation_id=conversation.id, messages=RelationCollection([summary])
    )
    async with async_session() as session:
        session.add(compaction)
        await session.commit()
    before = [uuid7()] if invalid == "input" else [original.id]
    after = [uuid7()] if invalid == "replacement" else [summary.id]
    if invalid == "duplicate":
        after.append(summary.id)
    error = {
        "input": "effective context",
        "replacement": "belong to this compaction",
        "duplicate": "must be unique",
    }[invalid]
    with pytest.raises(ValueError, match=error):
        await manager.record_compaction_memberships(
            conversation, [CompactionRewrite(compaction.id, before, after)]
        )
    async with async_session() as session:
        saved = await session.get(Compaction, compaction.id)
        assert saved is not None
        assert not saved.membership_complete
        assert await session.count(CompactionMembership) == 0
