from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from octomate.agents.base import SessionContext
from octomate.stores.message import MessageStore
from octomate.transmuters.messages import Message


def history_toolset() -> FunctionToolset[SessionContext]:
    toolset = FunctionToolset[SessionContext]()
    store = MessageStore()

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
        return await store.search(ctx.deps.session_key, query, offset, limit)

    return toolset
