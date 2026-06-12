"""TodoCapability: persists via the manager and emits granular todo events on the
stream through `Agent.stream_events`.

TestModel drives the create path end-to-end. Status-change/completed/deleted
derivation and stream injection are covered deterministically as units, because
TestModel generates random tool args (so it can't target a real todo `ref`) and
FunctionModel's streaming form is fiddly to script for one assertion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import (
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoStatusChangedEvent,
)
from octomate.capabilities.todos import TodoCapability, update_events
from octomate.managers.conversations import ConversationManager
from octomate.managers.todos import TodoManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.todos import Todo


async def _conversation() -> Conversation:
    return await ConversationManager().ensure(
        ConversationKey(
            channel_tentacle_id="slack",
            chat_type="private",
            chat_id="alice",
            user_id="alice",
        ),
        agent_tentacle_id="agent",
    )


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
