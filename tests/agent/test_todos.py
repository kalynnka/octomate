"""MCP todos persist in the calling conversation and publish each mutation once."""

from fastmcp import Client
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoStatusChangedEvent,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.gateway import OctomateSession
from octomate.managers.todos import TodoManager
from octomate.mcp.todos import update_events
from octomate.nerve import ChannelTopic, Nerve
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.todos import Todo
from tests.support.managers import FakeThreadManager, a_thread, a_workspace_manager
from tests.support.mcp import core_tool_server
from tests.support.nerve import held


async def a_session() -> OctomateSession:
    threads = FakeThreadManager()
    conversation = await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="agent"
    )
    return OctomateSession(
        channel_routes={},
        current_agent_id="agent",
        conversation_id=conversation.id,
        conversation_address=ChannelAddress("im", "dm", "alice", "alice"),
        nerve=Nerve(),
        users=threads.users,
        threads=threads,
        workspaces=a_workspace_manager(),
    )


async def test_add_todo_emits_created_event_and_persists(
    in_memory_engine: AsyncEngine,
) -> None:
    session = await a_session()
    assert session.nerve is not None
    assert session.conversation_id is not None
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session)) as client,
    ):
        result = await client.call_tool(
            "add_todo", {"content": "Run tests", "active_form": "Running tests"}
        )
        [created] = [impulse.event for impulse in held(events)]
    assert isinstance(created, TodoCreatedEvent)
    persisted = await TodoManager().list_todos(session.conversation_id)
    assert len(persisted) == 1
    assert persisted[0].ref == created.todo.ref
    assert persisted[0].content == "Run tests"
    assert "Added" in result.data


def test_update_events_derivation() -> None:
    cid = uuid7()
    pending = Todo(conversation_id=cid, ref="r1", content="x", status="pending")
    started = Todo(conversation_id=cid, ref="r1", content="x", status="in_progress")
    done = Todo(conversation_id=cid, ref="r1", content="x", status="completed")

    assert [type(e).__name__ for e in update_events(started, started)] == [
        "TodoUpdatedEvent"
    ]
    assert [type(e).__name__ for e in update_events(started, pending)] == [
        "TodoUpdatedEvent",
        "TodoStatusChangedEvent",
    ]
    assert [type(e).__name__ for e in update_events(done, started)] == [
        "TodoUpdatedEvent",
        "TodoStatusChangedEvent",
        "TodoCompletedEvent",
    ]


async def test_status_change_publishes_update_status_and_completion_once(
    in_memory_engine: AsyncEngine,
) -> None:
    session = await a_session()
    assert session.nerve is not None
    assert session.conversation_id is not None
    todo = await TodoManager().add_todo(
        session.conversation_id, content="test", active_form="testing"
    )
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session)) as client,
    ):
        await client.call_tool(
            "update_todo_status", {"todo_id": todo.ref, "status": "completed"}
        )
        changed = [impulse.event for impulse in held(events)]
    assert [type(event).__name__ for event in changed] == [
        "TodoUpdatedEvent",
        "TodoStatusChangedEvent",
        "TodoCompletedEvent",
    ]
    assert isinstance(changed[1], TodoStatusChangedEvent)
    assert isinstance(changed[2], TodoCompletedEvent)
    assert changed[2].todo.status == "completed"
    stored = await TodoManager().get_todo(session.conversation_id, todo.ref)
    assert stored is not None
    assert stored.status == "completed"


async def test_write_todos_emits_diff_events(in_memory_engine: AsyncEngine) -> None:
    session = await a_session()
    assert session.nerve is not None
    assert session.conversation_id is not None
    manager = TodoManager()
    old = await manager.add_todo(
        session.conversation_id, content="old", active_form="olding"
    )
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session)) as client,
    ):
        await client.call_tool(
            "write_todos", {"todos": [{"content": "new", "active_form": "renewing"}]}
        )
        deleted, created = [impulse.event for impulse in held(events)]
    assert isinstance(deleted, TodoDeletedEvent)
    assert deleted.todo.ref == old.ref
    assert isinstance(created, TodoCreatedEvent)
    persisted = await manager.list_todos(session.conversation_id)
    assert {todo.ref for todo in persisted} == {created.todo.ref}


async def test_set_dependency_rejects_cycles(in_memory_engine: AsyncEngine) -> None:
    session = await a_session()
    assert session.conversation_id is not None
    manager = TodoManager()
    first = await manager.add_todo(
        session.conversation_id, content="first", active_form="firsting"
    )
    second = await manager.add_todo(
        session.conversation_id, content="second", active_form="seconding"
    )
    async with Client(core_tool_server(session)) as client:
        forward = await client.call_tool(
            "set_dependency", {"todo_id": second.ref, "depends_on_id": first.ref}
        )
        assert "now depends on" in forward.data
        rejected = await client.call_tool(
            "set_dependency", {"todo_id": first.ref, "depends_on_id": second.ref}
        )
    assert rejected.data == "Cannot add dependency: it would create a cycle."
    stored = await manager.get_todo(session.conversation_id, first.ref)
    assert stored is not None
    assert stored.depends_on == []


async def test_get_available_tasks_excludes_blocked(
    in_memory_engine: AsyncEngine,
) -> None:
    session = await a_session()
    assert session.conversation_id is not None
    manager = TodoManager()
    blocker = await manager.add_todo(
        session.conversation_id, content="dig", active_form="digging"
    )
    blocked = await manager.add_todo(
        session.conversation_id, content="fill", active_form="filling"
    )
    await manager.update_todo(
        session.conversation_id, blocked.ref, depends_on=[blocker.ref]
    )
    async with Client(core_tool_server(session)) as client:
        rendered = (await client.call_tool("get_available_tasks", {})).data
        assert blocker.ref in rendered
        assert blocked.ref not in rendered
        await manager.update_todo(
            session.conversation_id, blocker.ref, status="completed"
        )
        assert blocked.ref in (await client.call_tool("get_available_tasks", {})).data
