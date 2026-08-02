"""History capability: thread-ledger search plus explicit model-ledger tools.

Thread history is the user-facing thread ledger (`ThreadMessage`) and is the
surface for `search_thread_history`. Model history is the agent conversation's
`ModelMessage` stream and is available under explicit model-history names.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    FunctionToolset,
)

from octomate.managers import ConversationManager, ThreadManager
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from octomate.schemas.thread import ChannelActorKind, ThreadMessage

MODEL_HISTORY_INSTRUCTIONS = """\
## Searching history

You can inspect this agent conversation's model ledger:

- `search_model_history(query[, role])` searches your prompts, assistant
  responses, tool calls, tool results, retries, and thinking.
- `read_model_history_before(model_message_id)` /
  `read_model_history_after(...)` page model messages around a model-history
  result.
"""

THREAD_HISTORY_INSTRUCTIONS = """
- Thread history is what people, bots, and agents visibly said in this thread,
  including messages that did not wake you.
- `search_thread_history(query[, actor_kind])` searches thread history. Prefer
  it when the user refers to what people said.
- `read_thread_history_before(thread_message_id)` /
  `read_thread_history_after(...)` page thread messages around a thread-history
  result.
- `read_related_model_messages(thread_message_id)` and
  `read_related_chat_messages(model_message_id)` jump between the two ledgers.
"""

HISTORY_INSTRUCTIONS = MODEL_HISTORY_INSTRUCTIONS + THREAD_HISTORY_INSTRUCTIONS


def conversation_id(ctx: object) -> uuid.UUID:
    value = getattr(ctx, "conversation_id", None)
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        return uuid.UUID(value)
    if value is None:
        raise ValueError("history tools require a conversation_id on the run")
    raise TypeError("history tools require a UUID conversation_id")


async def thread_id(
    ctx: object, conversation_manager: ConversationManager
) -> uuid.UUID:
    value = await conversation_manager.thread_id(conversation_id(ctx))
    if value is not None:
        return value
    raise ValueError("thread history tools require a thread-backed conversation")


def build_thread_history_toolset(
    conversation_manager: ConversationManager,
    thread_manager: ThreadManager,
) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id="thread_history")

    @toolset.tool
    async def search_thread_history(
        ctx: RunContext[Any],
        query: str,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
    ) -> list[ThreadMessage]:
        """Find visible thread messages whose text contains `query`
        (case-insensitive). Optionally restrict to an actor kind such as
        "human", "agent", "bot", or "system"."""
        return await thread_manager.search_chat_messages(
            await thread_id(ctx, conversation_manager),
            query,
            actor_kind=actor_kind,
            limit=limit,
        )

    @toolset.tool
    async def read_thread_history_before(
        ctx: RunContext[Any], message_id: str, limit: int = 10
    ) -> list[ThreadMessage]:
        """The visible thread messages immediately preceding `message_id` in this
        thread, oldest first."""
        return await thread_manager.chat_messages_before(
            await thread_id(ctx, conversation_manager),
            uuid.UUID(message_id),
            limit=limit,
        )

    @toolset.tool
    async def read_thread_history_after(
        ctx: RunContext[Any], message_id: str, limit: int = 10
    ) -> list[ThreadMessage]:
        """The visible thread messages immediately following `message_id` in this
        thread, oldest first."""
        return await thread_manager.chat_messages_after(
            await thread_id(ctx, conversation_manager),
            uuid.UUID(message_id),
            limit=limit,
        )

    @toolset.tool
    async def read_related_model_messages(
        ctx: RunContext[Any], thread_message_id: str
    ) -> list[ModelRequest | ModelResponse]:
        """Model-ledger messages related to a visible chat message."""
        await thread_id(ctx, conversation_manager)
        return await thread_manager.related_model_messages(uuid.UUID(thread_message_id))

    @toolset.tool
    async def read_related_chat_messages(
        ctx: RunContext[Any], model_message_id: str
    ) -> list[ThreadMessage]:
        """Visible thread messages related to a model-ledger message."""
        await thread_id(ctx, conversation_manager)
        return await conversation_manager.related_chat_messages(
            uuid.UUID(model_message_id)
        )

    return toolset


def build_model_history_toolset(
    conversation_manager: ConversationManager,
) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id="model_history")

    @toolset.tool
    async def search_model_history(
        ctx: RunContext[Any],
        query: str,
        role: Literal["user", "assistant"] | None = None,
        limit: int = 10,
    ) -> list[ModelMessage]:
        """Find model-ledger messages in this agent conversation whose text
        contains `query` (case-insensitive). Optionally restrict by role."""
        return await conversation_manager.search_messages(
            conversation_id(ctx), query, role=role, limit=limit
        )

    @toolset.tool
    async def read_model_history_before(
        ctx: RunContext[Any], model_message_id: str, limit: int = 10
    ) -> list[ModelMessage]:
        """Model-ledger messages immediately preceding `model_message_id` in this
        agent conversation, oldest first."""
        return await conversation_manager.messages_before(
            conversation_id(ctx), uuid.UUID(model_message_id), limit=limit
        )

    @toolset.tool
    async def read_model_history_after(
        ctx: RunContext[Any], model_message_id: str, limit: int = 10
    ) -> list[ModelMessage]:
        """Model-ledger messages immediately following `model_message_id` in this
        agent conversation, oldest first."""
        return await conversation_manager.messages_after(
            conversation_id(ctx), uuid.UUID(model_message_id), limit=limit
        )

    return toolset


@dataclass
class HistoryCapability(AbstractCapability[Any]):
    """Adds thread-history and model-history tools scoped to the run's conversation."""

    conversation_manager: ConversationManager
    # No default: the capability must read the host's ledger manager, never a
    # private one with its own identity registry.
    thread_manager: ThreadManager
    toolset: AbstractToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.toolset = CombinedToolset(
            (
                build_model_history_toolset(self.conversation_manager),
                build_thread_history_toolset(
                    self.conversation_manager,
                    self.thread_manager,
                ),
            )
        )

    def get_toolset(self) -> AbstractToolset[Any] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return HISTORY_INSTRUCTIONS
