"""Conversation-scoped todos served through the authenticated Octomate session."""

from collections import Counter
from dataclasses import dataclass, field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import UUID7

from octomate.events import (
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.managers.gateway import OctomateSession
from octomate.managers.todos import TodoManager
from octomate.mcp.base import capability_contract
from octomate.schemas.todos import Todo, TodoItem, TodoWrite
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


def todo_instructions() -> str:
    return TODO_INSTRUCTION


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
                lines.append(
                    f"{indent}{counter[0]}. {marker} [{todo.ref}] {todo.content}"
                )
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


@dataclass
class TodoTools:
    """Conversation-scoped todo tools and the events their mutations produce."""

    conversation_id: UUID7
    manager: TodoManager = field(default_factory=TodoManager)

    async def read_todos(self, hierarchical: bool = False) -> str:
        """List the current todos with their ids and statuses."""
        todos = await self.manager.list_todos(self.conversation_id)
        if not todos:
            return "No todos yet. Use write_todos or add_todo to create some."
        return render_todos(todos, hierarchical=hierarchical)

    async def write_todos(self, todos: list[TodoItem]) -> tuple[str, list[TodoEvent]]:
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
        deleted, created = await self.manager.write_todos(self.conversation_id, items)
        events: list[TodoEvent] = [TodoDeletedEvent(todo=todo) for todo in deleted]
        events += [TodoCreatedEvent(todo=todo) for todo in created]
        return (f"Wrote {len(created)} todos.", events)

    async def add_todo(
        self, content: str, active_form: str
    ) -> tuple[str, list[TodoEvent]]:
        """Append a single new todo (status pending)."""
        todo = await self.manager.add_todo(
            self.conversation_id, content=content, active_form=active_form
        )
        return (f"Added '{content}' (id: {todo.ref}).", [TodoCreatedEvent(todo=todo)])

    async def update_todo_status(
        self, todo_id: str, status: TodoStatus
    ) -> str | tuple[str, list[TodoEvent]]:
        """Change a todo's status by id."""
        conv = self.conversation_id
        previous = await self.manager.get_todo(conv, todo_id)
        if previous is None:
            return f"No todo with id '{todo_id}'."
        if status == "in_progress":
            if is_blocked(previous, by_ref(await self.manager.list_todos(conv))):
                return (
                    f"Cannot start '{previous.content}' — it has incomplete "
                    "dependencies."
                )
        updated = await self.manager.update_todo(conv, todo_id, status=status)
        if updated is None:
            return f"No todo with id '{todo_id}'."
        return (
            f"Updated '{updated.content}' → {status}.",
            update_events(updated, previous),
        )

    async def remove_todo(self, todo_id: str) -> str | tuple[str, list[TodoEvent]]:
        """Delete a todo by id."""
        removed = await self.manager.remove_todo(self.conversation_id, todo_id)
        if removed is None:
            return f"No todo with id '{todo_id}'."
        return (f"Removed '{removed.content}'.", [TodoDeletedEvent(todo=removed)])

    async def add_subtask(
        self, parent_id: str, content: str, active_form: str
    ) -> str | tuple[str, list[TodoEvent]]:
        """Add a subtask under an existing todo."""
        conv = self.conversation_id
        parent = await self.manager.get_todo(conv, parent_id)
        if parent is None:
            return f"No parent todo with id '{parent_id}'."
        todo = await self.manager.add_todo(
            conv, content=content, active_form=active_form, parent_ref=parent_id
        )
        return (
            f"Added subtask '{content}' (id: {todo.ref}, parent: {parent_id}).",
            [TodoCreatedEvent(todo=todo)],
        )

    async def set_dependency(
        self, todo_id: str, depends_on_id: str
    ) -> str | tuple[str, list[TodoEvent]]:
        """Make `todo_id` depend on `depends_on_id` (blocks it until that completes)."""
        conv = self.conversation_id
        todos = await self.manager.list_todos(conv)
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
        updated = await self.manager.update_todo(
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
        return (message, update_events(updated, todo))

    async def get_available_tasks(self) -> str:
        """List tasks with no incomplete dependencies (workable now)."""
        available = available_tasks(await self.manager.list_todos(self.conversation_id))
        if not available:
            return "No available tasks — all are completed or blocked."
        lines = ["Available tasks:"]
        for index, todo in enumerate(available, 1):
            marker = STATUS_MARKERS.get(todo.status, "[ ]")
            lines.append(f"{index}. {marker} [{todo.ref}] {todo.content}")
        return "\n".join(lines)


def mount_todos(mcp: FastMCP, octomate_session: OctomateSession) -> None:
    """Derive the todo owner from the verified session, never tool arguments."""

    def tools(session: OctomateSession) -> TodoTools:
        if session.conversation_id is None or session.conversation_address is None:
            raise ToolError("Todo tools require an active conversation.")
        return TodoTools(session.conversation_id)

    async def publish(
        session: OctomateSession, result: str | tuple[str, list[TodoEvent]]
    ) -> str:
        if isinstance(result, str):
            return result
        address = session.conversation_address
        if address is None:
            raise RuntimeError("A todo mutation has no conversation address")
        message, events = result
        for event in events:
            await session.publish(address, event)
        return message

    @mcp.tool(description=capability_contract(TodoTools.read_todos))
    async def read_todos(
        hierarchical: bool = False, session: OctomateSession = octomate_session
    ) -> str:
        return await tools(session).read_todos(hierarchical)

    @mcp.tool(description=capability_contract(TodoTools.write_todos))
    async def write_todos(
        todos: list[TodoItem], session: OctomateSession = octomate_session
    ) -> str:
        return await publish(session, await tools(session).write_todos(todos))

    @mcp.tool(description=capability_contract(TodoTools.add_todo))
    async def add_todo(
        content: str, active_form: str, session: OctomateSession = octomate_session
    ) -> str:
        return await publish(
            session, await tools(session).add_todo(content, active_form)
        )

    @mcp.tool(description=capability_contract(TodoTools.update_todo_status))
    async def update_todo_status(
        todo_id: str, status: TodoStatus, session: OctomateSession = octomate_session
    ) -> str:
        return await publish(
            session, await tools(session).update_todo_status(todo_id, status)
        )

    @mcp.tool(description=capability_contract(TodoTools.remove_todo))
    async def remove_todo(
        todo_id: str, session: OctomateSession = octomate_session
    ) -> str:
        return await publish(session, await tools(session).remove_todo(todo_id))

    @mcp.tool(description=capability_contract(TodoTools.add_subtask))
    async def add_subtask(
        parent_id: str,
        content: str,
        active_form: str,
        session: OctomateSession = octomate_session,
    ) -> str:
        return await publish(
            session, await tools(session).add_subtask(parent_id, content, active_form)
        )

    @mcp.tool(description=capability_contract(TodoTools.set_dependency))
    async def set_dependency(
        todo_id: str, depends_on_id: str, session: OctomateSession = octomate_session
    ) -> str:
        return await publish(
            session, await tools(session).set_dependency(todo_id, depends_on_id)
        )

    @mcp.tool(description=capability_contract(TodoTools.get_available_tasks))
    async def get_available_tasks(session: OctomateSession = octomate_session) -> str:
        return await tools(session).get_available_tasks()
