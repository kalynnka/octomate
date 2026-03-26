from __future__ import annotations

from arcanus.materia.sqlalchemy import AsyncSession
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from octomate.agents.base import SessionContext
from octomate.database import engine
from octomate.transmuters.messages import Message


def history_toolset() -> FunctionToolset[SessionContext]:
    toolset = FunctionToolset[SessionContext]()

    @toolset.tool
    async def search_messages(
        ctx: RunContext[SessionContext],
        query: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> list[Message]:
        """Search past messages in the current conversation.

        Results are scoped to the current tentacle, chat, and thread.
        Returns messages in reverse chronological order with offset pagination.

        Args:
            query: Text to search for in message content. Empty string returns all messages.
            offset: Number of messages to skip for pagination. 0 for the first page.
            limit: Maximum number of messages to return (1-50).
        """
        key = ctx.deps.session_key
        chat = key.group_id or key.user_id
        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        expressions = [
            Message["tentacle_id"] == key.tentacle_id,
            Message["chat"] == chat,
        ]

        if key.thread_id:
            expressions.append(Message["thread_id"] == key.thread_id)
        else:
            expressions.append(Message["thread_id"].is_(None))

        if query:
            expressions.append(Message["content"].contains(query))

        async with AsyncSession(engine()) as session:
            return list(
                await session.list(
                    Message,
                    expressions=expressions,
                    order_bys=[Message["timestamp"].desc(), Message["id"].desc()],
                    limit=limit,
                    offset=offset,
                )
            )

    return toolset
