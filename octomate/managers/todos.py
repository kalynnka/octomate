from __future__ import annotations

import uuid
from datetime import datetime, timezone

from octomate.database import async_session
from octomate.schemas.todos import Todo, TodoWrite
from octomate.types.todos import TodoStatus


def fresh_ref(taken: set[str]) -> str:
    """A short hex todo handle not already in `taken`."""
    ref = uuid.uuid4().hex[:8]
    while ref in taken:
        ref = uuid.uuid4().hex[:8]
    return ref


class TodoManager:
    """Conversation-scoped todo persistence, keyed on the short hex `ref`.

    Persistence only — event derivation and cycle/blocking validation live in the
    todo capability, which owns the agent-facing behaviour.
    """

    async def list_todos(self, conversation_id: uuid.UUID) -> list[Todo]:
        async with async_session() as session:
            todos = await session.list(
                Todo,
                limit=None,
                order_bys=[Todo["position"], Todo["created_at"]],
                expressions=[Todo["conversation_id"] == conversation_id],
            )
        return list(todos)

    async def get_todo(self, conversation_id: uuid.UUID, ref: str) -> Todo | None:
        async with async_session() as session:
            return await session.one_or_none(
                Todo,
                expressions=[
                    Todo["conversation_id"] == conversation_id,
                    Todo["ref"] == ref,
                ],
            )

    async def add_todo(
        self,
        conversation_id: uuid.UUID,
        *,
        content: str,
        active_form: str,
        parent_ref: str | None = None,
    ) -> Todo:
        existing = await self.list_todos(conversation_id)
        todo = Todo(
            conversation_id=conversation_id,
            ref=fresh_ref({item.ref for item in existing}),
            content=content,
            active_form=active_form,
            parent_ref=parent_ref,
            position=max((item.position for item in existing), default=-1) + 1,
        )
        async with async_session() as session:
            session.add(todo)
            await session.commit()
        return todo

    async def update_todo(
        self,
        conversation_id: uuid.UUID,
        ref: str,
        *,
        content: str | None = None,
        status: TodoStatus | None = None,
        active_form: str | None = None,
        parent_ref: str | None = None,
        depends_on: list[str] | None = None,
    ) -> Todo | None:
        async with async_session() as session:
            todo = await session.one_or_none(
                Todo,
                expressions=[
                    Todo["conversation_id"] == conversation_id,
                    Todo["ref"] == ref,
                ],
            )
            if todo is None:
                return None
            if content is not None:
                todo.content = content
            if status is not None:
                todo.status = status
            if active_form is not None:
                todo.active_form = active_form
            if parent_ref is not None:
                todo.parent_ref = parent_ref
            if depends_on is not None:
                todo.depends_on = depends_on
            todo.updated_at = datetime.now(timezone.utc)
            await session.commit()
        return todo

    async def remove_todo(self, conversation_id: uuid.UUID, ref: str) -> Todo | None:
        async with async_session() as session:
            todo = await session.one_or_none(
                Todo,
                expressions=[
                    Todo["conversation_id"] == conversation_id,
                    Todo["ref"] == ref,
                ],
            )
            if todo is None:
                return None
            await session.delete(todo)
            await session.commit()
        return todo

    async def write_todos(
        self,
        conversation_id: uuid.UUID,
        items: list[TodoWrite],
    ) -> tuple[list[Todo], list[Todo]]:
        """Replace the conversation's todos with `items`; return (deleted, created)."""
        async with async_session() as session:
            deleted = list(
                await session.list(
                    Todo,
                    limit=None,
                    order_bys=[Todo["position"], Todo["created_at"]],
                    expressions=[Todo["conversation_id"] == conversation_id],
                )
            )
            for todo in deleted:
                await session.delete(todo)
            # Flush the deletes before inserting so a reused `ref` cannot trip the
            # (conversation_id, ref) unique constraint mid-flush.
            await session.commit()

            taken: set[str] = set()
            created: list[Todo] = []
            for position, item in enumerate(items):
                ref = item.get("ref") or fresh_ref(taken)
                taken.add(ref)
                todo = Todo(
                    conversation_id=conversation_id,
                    ref=ref,
                    content=item["content"],
                    status=item.get("status", "pending"),
                    active_form=item.get("active_form", ""),
                    parent_ref=item.get("parent_ref"),
                    depends_on=item.get("depends_on", []),
                    position=position,
                )
                session.add(todo)
                created.append(todo)
            await session.commit()
        return deleted, created
