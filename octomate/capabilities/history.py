"""History capability: search this conversation's past messages and page around hits.

The agent searches the durable model-message store by the text users and the
assistant actually said (`message_text`). Thinking and tool calls carry no text
and are not searchable, but they sit in the same ordered stream — so once a
message is located, the agent reaches its non-text neighbours by paging before
and after it.

Read-only: the tools query the shared `ConversationManager` and return the
matched / neighbouring messages directly; pydantic-ai serializes them for the
model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.managers import ConversationManager
from octomate.schemas.messages import ModelMessage

HISTORY_INSTRUCTION = """\
## Searching this conversation's history

You can look back over everything said earlier in *this* conversation:

- `search_history(query[, role])` — find past messages whose text contains
  `query` (case-insensitive). Only what the user and you actually *said* is
  searchable; restrict with `role="user"` or `role="assistant"` when helpful.
- `read_history_before(message_id)` / `read_history_after(message_id)` — page the
  neighbouring messages around a result's `id`. These include the tool calls and
  reasoning that aren't searchable on their own, so use them to recover the
  context around a hit, then shift further by paging from an edge message's id.

Prefer this over guessing when the user refers to something from earlier.
"""


def build_history_toolset(manager: ConversationManager) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id="history")

    def conversation_id(ctx: RunContext[Any]) -> uuid.UUID:
        if ctx.conversation_id is None:
            raise ValueError("history tools require a conversation_id on the run")
        return uuid.UUID(ctx.conversation_id)

    @toolset.tool
    async def search_history(
        ctx: RunContext[Any],
        query: str,
        role: Literal["user", "assistant"] | None = None,
        limit: int = 10,
    ) -> list[ModelMessage]:
        """Find earlier messages in this conversation whose text contains `query`
        (case-insensitive). Only messages the user or assistant actually said are
        searchable; optionally restrict to a `role`. Each result carries an `id`
        for read_history_before / read_history_after to see its neighbours."""
        return await manager.search_messages(
            conversation_id(ctx), query, role=role, limit=limit
        )

    @toolset.tool
    async def read_history_before(
        ctx: RunContext[Any], message_id: str, limit: int = 10
    ) -> list[ModelMessage]:
        """The messages immediately preceding `message_id` in this conversation
        (oldest first), including non-text tool-call and reasoning messages."""
        return await manager.messages_before(
            conversation_id(ctx), uuid.UUID(message_id), limit=limit
        )

    @toolset.tool
    async def read_history_after(
        ctx: RunContext[Any], message_id: str, limit: int = 10
    ) -> list[ModelMessage]:
        """The messages immediately following `message_id` in this conversation
        (oldest first), including non-text tool-call and reasoning messages."""
        return await manager.messages_after(
            conversation_id(ctx), uuid.UUID(message_id), limit=limit
        )

    return toolset


@dataclass
class HistoryCapability(AbstractCapability[Any]):
    """Adds the history search/pagination tools, scoped to the run's conversation.

    Uses the shared `ConversationManager` so it reads the same message store the
    agent's working history is loaded from."""

    conversation_manager: ConversationManager
    toolset: FunctionToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.toolset = build_history_toolset(self.conversation_manager)

    def get_toolset(self) -> AbstractToolset[Any] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return HISTORY_INSTRUCTION
