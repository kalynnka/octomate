"""TodoManager: conversation-scoped persistence keyed on the short hex `ref`."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.managers.conversation import ConversationManager
from octomate.managers.todos import TodoManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.todos import TodoWrite


async def _conversation(chat_id: str = "alice") -> Conversation:
    return await ConversationManager().ensure(
        uuid.uuid5(uuid.NAMESPACE_OID, chat_id),
        agent_tentacle_id="agent",
    )


async def test_add_and_list_preserves_order(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    first = await manager.add_todo(conv.id, content="first", active_form="doing first")
    second = await manager.add_todo(conv.id, content="second", active_form="doing 2nd")

    todos = await manager.list_todos(conv.id)
    assert [todo.content for todo in todos] == ["first", "second"]
    assert [todo.position for todo in todos] == [0, 1]
    assert first.ref != second.ref


async def test_update_mutates_in_place(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    todo = await manager.add_todo(conv.id, content="ship", active_form="shipping")

    updated = await manager.update_todo(conv.id, todo.ref, status="completed")
    assert updated is not None
    assert updated.status == "completed"
    assert (await manager.get_todo(conv.id, todo.ref)).status == "completed"  # type: ignore[union-attr]

    assert await manager.update_todo(conv.id, "missing", status="completed") is None


async def test_remove_returns_removed(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    todo = await manager.add_todo(conv.id, content="drop me", active_form="dropping")

    removed = await manager.remove_todo(conv.id, todo.ref)
    assert removed is not None
    assert removed.ref == todo.ref
    assert await manager.list_todos(conv.id) == []
    assert await manager.remove_todo(conv.id, todo.ref) is None


async def test_write_replaces_and_reports_diff(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    await manager.add_todo(conv.id, content="old", active_form="olding")

    items: list[TodoWrite] = [
        TodoWrite(content="a", active_form="a-ing", status="in_progress"),
        TodoWrite(content="b", active_form="b-ing"),
    ]
    deleted, created = await manager.write_todos(conv.id, items)

    assert [todo.content for todo in deleted] == ["old"]
    assert [todo.content for todo in created] == ["a", "b"]
    assert [todo.status for todo in created] == ["in_progress", "pending"]
    assert [todo.content for todo in await manager.list_todos(conv.id)] == ["a", "b"]


async def test_scoped_by_conversation(in_memory_engine: AsyncEngine) -> None:
    alice = await _conversation("alice")
    bob = await _conversation("bob")
    manager = TodoManager()
    await manager.add_todo(alice.id, content="alice task", active_form="...")

    assert len(await manager.list_todos(alice.id)) == 1
    assert await manager.list_todos(bob.id) == []
