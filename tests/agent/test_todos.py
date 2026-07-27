"""TodoCapability: persists via the manager and emits granular todo events on the
stream through `Agent.stream_events`.

TestModel drives the create path end-to-end. Status-change/completed/deleted
derivation and stream injection are covered deterministically as units, because
TestModel generates random tool args (so it can't target a real todo `ref`) and
FunctionModel's streaming form is fiddly to script for one assertion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import (
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoStatusChangedEvent,
)
from octomate.capabilities.todos import TodoCapability, update_events
from octomate.managers.conversation import ConversationManager
from octomate.managers.todos import TodoManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.todos import Todo


async def _conversation() -> Conversation:
    return await ConversationManager().ensure(
        uuid7(),
        agent_tentacle_id="agent",
    )


def _tool_ctx(conversation: Conversation) -> RunContext[Any]:
    """The todo tools only read `ctx.conversation_id` — one dynamic-boundary cast."""
    return cast(RunContext[Any], SimpleNamespace(conversation_id=str(conversation.id)))


async def test_add_todo_emits_created_event_and_persists(
    in_memory_engine: AsyncEngine,
) -> None:
    conv = await _conversation()
    manager = TodoManager()
    agent = Agent(
        TestModel(call_tools=["add_todo"]),
        capabilities=[TodoCapability(manager=manager)],
    )

    events = [
        event
        async for event in agent.stream_events("plan it", conversation_id=str(conv.id))
    ]

    created = [event for event in events if isinstance(event, TodoCreatedEvent)]
    assert created, "expected a TodoCreatedEvent emitted by add_todo"
    persisted = await manager.list_todos(conv.id)
    assert len(persisted) == 1
    assert persisted[0].ref == created[0].todo.ref


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


async def test_wrap_forwards_stashed_todo_events() -> None:
    cid = uuid7()
    todo = Todo(conversation_id=cid, ref="r1", content="x", status="completed")
    stashed = [TodoStatusChangedEvent(todo=todo), TodoCompletedEvent(todo=todo)]
    result_event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="update_todo_status",
            content="ok",
            tool_call_id="c1",
            metadata=stashed,
        )
    )

    async def fake_stream() -> AsyncIterator[AgentStreamEvent]:
        yield result_event

    capability = TodoCapability(manager=TodoManager())
    out = [
        event
        async for event in capability.wrap_run_event_stream(
            cast(RunContext[Any], None), stream=fake_stream()
        )
    ]

    assert out[0] is result_event
    assert out[1:] == stashed


async def test_write_todos_emits_diff_events(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    old = await manager.add_todo(conv.id, content="old", active_form="olding")
    agent = Agent(
        TestModel(call_tools=["write_todos"]),
        capabilities=[TodoCapability(manager=manager)],
    )

    events = [
        event
        async for event in agent.stream_events("replan", conversation_id=str(conv.id))
    ]

    deleted = [event for event in events if isinstance(event, TodoDeletedEvent)]
    created = [event for event in events if isinstance(event, TodoCreatedEvent)]
    assert [event.todo.ref for event in deleted] == [old.ref]
    assert created, "expected TodoCreatedEvents for the written todos"
    persisted = await manager.list_todos(conv.id)
    assert {todo.ref for todo in persisted} == {event.todo.ref for event in created}


async def test_set_dependency_rejects_cycles(in_memory_engine: AsyncEngine) -> None:
    conv = await _conversation()
    manager = TodoManager()
    capability = TodoCapability(manager=manager)
    assert capability.toolset is not None
    set_dependency = capability.toolset.tools["set_dependency"].function
    ctx = _tool_ctx(conv)

    first = await manager.add_todo(conv.id, content="first", active_form="firsting")
    second = await manager.add_todo(conv.id, content="second", active_form="seconding")

    forward = await set_dependency(ctx, todo_id=second.ref, depends_on_id=first.ref)
    assert isinstance(forward, ToolReturn)

    rejected = await set_dependency(ctx, todo_id=first.ref, depends_on_id=second.ref)
    assert rejected == "Cannot add dependency: it would create a cycle."
    assert (await manager.get_todo(conv.id, first.ref)).depends_on == []  # type: ignore[union-attr]


async def test_get_available_tasks_excludes_blocked(
    in_memory_engine: AsyncEngine,
) -> None:
    conv = await _conversation()
    manager = TodoManager()
    capability = TodoCapability(manager=manager)
    assert capability.toolset is not None
    get_available_tasks = capability.toolset.tools["get_available_tasks"].function
    ctx = _tool_ctx(conv)

    blocker = await manager.add_todo(conv.id, content="dig", active_form="digging")
    blocked = await manager.add_todo(conv.id, content="fill", active_form="filling")
    await manager.update_todo(conv.id, blocked.ref, depends_on=[blocker.ref])

    rendered = await get_available_tasks(ctx)
    assert blocker.ref in rendered
    assert blocked.ref not in rendered

    await manager.update_todo(conv.id, blocker.ref, status="completed")
    rendered = await get_available_tasks(ctx)
    assert blocked.ref in rendered
