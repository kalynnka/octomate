"""Todo capability: a persisted, conversation-scoped task list the agent manages.

Models the `pydantic-ai-todo` toolset (read/write/add/update/remove + subtasks &
dependencies) but persists via Arcanus (`TodoManager`) and emits granular todo
events onto the octomate event stream instead of a private callback bus. The
capability holds the manager; each tool reads `ctx.conversation_id` for scoping,
so the agent's deps type is untouched. Tools stash the events they produce on
`ToolReturn.metadata`; `wrap_run_event_stream` forwards them onto the stream.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel, Field
from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.events import (
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.managers.todos import TodoManager
from octomate.schemas.todos import Todo, TodoWrite
from octomate.types.todos import STATUS_MARKERS, TodoStatus

TODO_TOOL_NAMES = frozenset(
    {
        "read_todos",
        "write_todos",
        "add_todo",
        "update_todo_status",
        "remove_todo",
        "add_subtask",
        "set_dependency",
        "get_available_tasks",
    }
)

TODO_INSTRUCTION = """\
## Task management

You have todo tools to plan and track multi-step work:
- `read_todos` — view the current tasks with their ids and statuses.
- `write_todos` — replace the whole list (use to lay out or restructure a plan).
- `add_todo` — append one task without disturbing the rest.
- `update_todo_status` — move a task between pending / in_progress / completed.
- `remove_todo` — delete a task by id.
- `add_subtask` / `set_dependency` / `get_available_tasks` — break tasks down,
  order them, and find what is unblocked.

Use them for non-trivial work (3+ steps). Keep exactly one task `in_progress`,
mark tasks `completed` immediately when done, and pass each task an imperative
`content` ("Run tests") plus a present-continuous `active_form` ("Running tests").
"""


class TodoItem(BaseModel):
    """One todo in a `write_todos` call (the model rewrites the whole list)."""

    content: str = Field(
        description="Imperative task title, e.g. 'Fix the login bug'.",
    )
    status: TodoStatus = Field(
        default="pending",
        description="pending, in_progress, completed, or blocked.",
    )
    active_form: str = Field(
        description="Present-continuous label, e.g. 'Fixing the login bug'.",
    )
    id: str | None = Field(
        default=None,
        description="Existing todo id to keep; omit to create a new one.",
    )
    parent_id: str | None = Field(
        default=None,
        description="Parent todo id when this is a subtask.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of todos that must complete before this one.",
    )


def by_ref(todos: list[Todo]) -> dict[str, Todo]:
    return {todo.ref: todo for todo in todos}


def is_blocked(todo: Todo, todos_by_ref: dict[str, Todo]) -> bool:
    return any(
        (dep := todos_by_ref.get(ref)) is not None and dep.status != "completed"
        for ref in todo.depends_on
    )


def has_cycle(todos: list[Todo], todo_ref: str, depends_on_ref: str) -> bool:
    """Would making `todo_ref` depend on `depends_on_ref` create a cycle?"""
    todos_by_ref = by_ref(todos)
    visited: set[str] = set()

    def visit(current: str) -> bool:
        if current == todo_ref:
            return True
        if current in visited:
            return False
        visited.add(current)
        dep = todos_by_ref.get(current)
        if dep is None:
            return False
        return any(visit(ref) for ref in dep.depends_on)

    return visit(depends_on_ref)


def available_tasks(todos: list[Todo]) -> list[Todo]:
    todos_by_ref = by_ref(todos)
    return [
        todo
        for todo in todos
        if todo.status not in ("completed", "blocked")
        and not is_blocked(todo, todos_by_ref)
    ]


def render_todos(todos: list[Todo], *, hierarchical: bool) -> str:
    if hierarchical:
        children: dict[str | None, list[Todo]] = {}
        for todo in todos:
            children.setdefault(todo.parent_ref, []).append(todo)
        lines = ["Current todos (tree):"]
        counter = [0]

        def walk(parent: str | None, depth: int) -> None:
            for todo in children.get(parent, []):
                counter[0] += 1
                indent = "  " * depth
                marker = STATUS_MARKERS.get(todo.status, "[ ]")
                lines.append(f"{indent}{counter[0]}. {marker} [{todo.ref}] {todo.content}")
                if todo.depends_on:
                    lines.append(f"{indent}   depends on {', '.join(todo.depends_on)}")
                walk(todo.ref, depth + 1)

        walk(None, 0)
    else:
        lines = ["Current todos:"]
        for index, todo in enumerate(todos, 1):
            marker = STATUS_MARKERS.get(todo.status, "[ ]")
            lines.append(f"{index}. {marker} [{todo.ref}] {todo.content}")
            if todo.parent_ref:
                lines.append(f"   (subtask of {todo.parent_ref})")
            if todo.depends_on:
                lines.append(f"   (depends on {', '.join(todo.depends_on)})")

    counts = Counter(todo.status for todo in todos)
    summary = (
        f"\nSummary: {counts['completed']} completed, {counts['blocked']} blocked, "
        f"{counts['in_progress']} in progress, {counts['pending']} pending"
    )
    return "\n".join(lines) + summary


def update_events(current: Todo, previous: Todo) -> list[TodoEvent]:
    events: list[TodoEvent] = [TodoUpdatedEvent(todo=current, previous=previous)]
    if current.status != previous.status:
        events.append(TodoStatusChangedEvent(todo=current, previous=previous))
        if current.status == "completed":
            events.append(TodoCompletedEvent(todo=current))
    return events


def build_todo_toolset(
    manager: TodoManager, *, enable_subtasks: bool
) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id="todo")

    def conversation_id(ctx: RunContext[Any]) -> uuid.UUID:
        if ctx.conversation_id is None:
            raise ValueError("todo tools require a conversation_id on the run")
        return uuid.UUID(ctx.conversation_id)

    @toolset.tool
    async def read_todos(ctx: RunContext[Any], hierarchical: bool = False) -> str:
        """List the current todos with their ids and statuses."""
        todos = await manager.list_todos(conversation_id(ctx))
        if not todos:
            return "No todos yet. Use write_todos or add_todo to create some."
        return render_todos(todos, hierarchical=hierarchical)

    @toolset.tool
    async def write_todos(
        ctx: RunContext[Any], todos: list[TodoItem]
    ) -> ToolReturn[str]:
        """Replace the whole todo list with `todos`."""
        items: list[TodoWrite] = [
            TodoWrite(
                content=item.content,
                status=item.status,
                active_form=item.active_form,
                ref=item.id,
                parent_ref=item.parent_id,
                depends_on=item.depends_on,
            )
            for item in todos
        ]
        deleted, created = await manager.write_todos(conversation_id(ctx), items)
        events: list[TodoEvent] = [TodoDeletedEvent(todo=todo) for todo in deleted]
        events += [TodoCreatedEvent(todo=todo) for todo in created]
        return ToolReturn(return_value=f"Wrote {len(created)} todos.", metadata=events)

    @toolset.tool
    async def add_todo(
        ctx: RunContext[Any], content: str, active_form: str
    ) -> ToolReturn[str]:
        """Append a single new todo (status pending)."""
        todo = await manager.add_todo(
            conversation_id(ctx), content=content, active_form=active_form
        )
        return ToolReturn(
            return_value=f"Added '{content}' (id: {todo.ref}).",
            metadata=[TodoCreatedEvent(todo=todo)],
        )

    @toolset.tool
    async def update_todo_status(
        ctx: RunContext[Any], todo_id: str, status: TodoStatus
    ) -> str | ToolReturn[str]:
        """Change a todo's status by id."""
        conv = conversation_id(ctx)
        previous = await manager.get_todo(conv, todo_id)
        if previous is None:
            return f"No todo with id '{todo_id}'."
        if enable_subtasks and status == "in_progress":
            if is_blocked(previous, by_ref(await manager.list_todos(conv))):
                return (
                    f"Cannot start '{previous.content}' — it has incomplete "
                    "dependencies."
                )
        updated = await manager.update_todo(conv, todo_id, status=status)
        if updated is None:
            return f"No todo with id '{todo_id}'."
        return ToolReturn(
            return_value=f"Updated '{updated.content}' → {status}.",
            metadata=update_events(updated, previous),
        )

    @toolset.tool
    async def remove_todo(
        ctx: RunContext[Any], todo_id: str
    ) -> str | ToolReturn[str]:
        """Delete a todo by id."""
        removed = await manager.remove_todo(conversation_id(ctx), todo_id)
        if removed is None:
            return f"No todo with id '{todo_id}'."
        return ToolReturn(
            return_value=f"Removed '{removed.content}'.",
            metadata=[TodoDeletedEvent(todo=removed)],
        )

    if enable_subtasks:

        @toolset.tool
        async def add_subtask(
            ctx: RunContext[Any], parent_id: str, content: str, active_form: str
        ) -> str | ToolReturn[str]:
            """Add a subtask under an existing todo."""
            conv = conversation_id(ctx)
            parent = await manager.get_todo(conv, parent_id)
            if parent is None:
                return f"No parent todo with id '{parent_id}'."
            todo = await manager.add_todo(
                conv, content=content, active_form=active_form, parent_ref=parent_id
            )
            return ToolReturn(
                return_value=f"Added subtask '{content}' (id: {todo.ref}, parent: {parent_id}).",
                metadata=[TodoCreatedEvent(todo=todo)],
            )

        @toolset.tool
        async def set_dependency(
            ctx: RunContext[Any], todo_id: str, depends_on_id: str
        ) -> str | ToolReturn[str]:
            """Make `todo_id` depend on `depends_on_id` (blocks it until that completes)."""
            conv = conversation_id(ctx)
            todos = await manager.list_todos(conv)
            todos_by_ref = by_ref(todos)
            todo = todos_by_ref.get(todo_id)
            dependency = todos_by_ref.get(depends_on_id)
            if todo is None:
                return f"No todo with id '{todo_id}'."
            if dependency is None:
                return f"No todo with id '{depends_on_id}'."
            if todo_id == depends_on_id:
                return "A todo cannot depend on itself."
            if has_cycle(todos, todo_id, depends_on_id):
                return "Cannot add dependency: it would create a cycle."
            if depends_on_id in todo.depends_on:
                return "That dependency already exists."
            new_status: TodoStatus = todo.status
            if dependency.status != "completed" and todo.status not in (
                "completed",
                "blocked",
            ):
                new_status = "blocked"
            updated = await manager.update_todo(
                conv,
                todo_id,
                depends_on=[*todo.depends_on, depends_on_id],
                status=new_status,
            )
            if updated is None:
                return f"No todo with id '{todo_id}'."
            message = f"'{todo.content}' now depends on '{dependency.content}'."
            if new_status == "blocked" and todo.status != "blocked":
                message += " Task blocked until the dependency completes."
            return ToolReturn(return_value=message, metadata=update_events(updated, todo))

        @toolset.tool
        async def get_available_tasks(ctx: RunContext[Any]) -> str:
            """List tasks with no incomplete dependencies (workable now)."""
            available = available_tasks(await manager.list_todos(conversation_id(ctx)))
            if not available:
                return "No available tasks — all are completed or blocked."
            lines = ["Available tasks:"]
            for index, todo in enumerate(available, 1):
                marker = STATUS_MARKERS.get(todo.status, "[ ]")
                lines.append(f"{index}. {marker} [{todo.ref}] {todo.content}")
            return "\n".join(lines)

    return toolset


@dataclass
class TodoCapability(AbstractCapability[Any]):
    """Adds the persisted todo toolset + its instruction, and emits granular todo
    events onto the stream as the model mutates the list."""

    manager: TodoManager = field(default_factory=TodoManager)
    enable_subtasks: bool = True
    toolset: FunctionToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.toolset = build_todo_toolset(
            self.manager, enable_subtasks=self.enable_subtasks
        )

    def get_toolset(self) -> AbstractToolset[Any] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return TODO_INSTRUCTION

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[Any],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        async for event in stream:
            yield event
            if (
                isinstance(event, FunctionToolResultEvent)
                and isinstance(event.part, ToolReturnPart)
                and event.part.tool_name in TODO_TOOL_NAMES
                and isinstance(event.part.metadata, list)
            ):
                for todo_event in event.part.metadata:
                    # The tool stashed our display events (not AgentStreamEvents) in
                    # metadata; inject them on the stream. One dynamic-boundary cast —
                    # pydantic-ai types the stream as AgentStreamEvent, consumers
                    # match the concrete octomate event type.
                    yield cast(AgentStreamEvent, todo_event)
