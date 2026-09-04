"""History capability: the thread ledger of the person a run answers.

Thread history is what people, bots and agents visibly said — `ThreadMessage` rows,
including the ones that did not wake this agent. The model ledger is not offered
here: it exists for audit and for rebuilding a conversation's context, not for a
model to read.

The scope is the person, not the thread: a run reads every thread the human it is
answering has spoken in, on any account the registry links to them — this thread,
their direct messages elsewhere, the chat a handoff came from. So the capability is
user-scoped, bound per run by `for_profile`, and no tool takes a conversation id.

The tools are methods with docstrings, as the gateway's spells are, so a runtime
that reaches them some other way than pydantic-ai projects the same contracts.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from octomate.managers import ConversationManager, ThreadManager
from octomate.schemas.thread import ChannelActorKind, ThreadMessage
from octomate.schemas.user import UserProfile

HISTORY_TOOLSET_ID = "history"

# The instruction prose, templated only where a tool is named, so each runtime's
# adapter renders one contract under its own tool naming — as the gateway's is.
HISTORY_INSTRUCTION_TEMPLATE = """\
## Searching history

Thread history is what people, bots, and agents visibly said — in this thread, and
in every other thread the person you are answering has spoken in, on any of their
linked accounts. Messages that did not wake you are there too.

- `{search_thread_history}` searches all of it for a substring, optionally by
  actor kind. Prefer it when the user refers to what was said, here or elsewhere.
- `{read_thread_history_before}` and `{read_thread_history_after}` page a thread
  around a search hit, or around a `#msg:<id>` handle a brief cited.
"""

# The tools, in the order the capability registers them — what a projection
# offers, and what an adapter pre-allows, named statically.
HISTORY_TOOLS: tuple[str, ...] = (
    "search_thread_history",
    "read_thread_history_before",
    "read_thread_history_after",
)


def history_instructions(tool_name: Callable[[str], str]) -> str:
    """The history instruction, each tool rendered by the caller's own naming."""
    return HISTORY_INSTRUCTION_TEMPLATE.format(
        **{name: tool_name(name) for name in HISTORY_TOOLS}
    )


HISTORY_INSTRUCTIONS = history_instructions(lambda name: name)


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


@dataclass
class HistoryCapability(AbstractCapability[Any]):
    """Thread-history tools over the history of the person a run answers.

    Mounted user-scoped: the tentacle asks `for_profile` for the copy bound to
    that run's user and mounts that, so no tool has to ask who is asking.
    """

    # No default: the capability must read the host's ledger manager, never a
    # private one with its own identity registry.
    thread_manager: ThreadManager
    # The person whose history the tools read. None on the mounted template,
    # which serves no run itself.
    profile: UserProfile | None = None
    toolset: FunctionToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        toolset: FunctionToolset[Any] = FunctionToolset(id=HISTORY_TOOLSET_ID)
        # No tool takes the run context: the person is bound in, not read off it.
        toolset.add_function(self.search_thread_history, takes_ctx=False)
        toolset.add_function(self.read_thread_history_before, takes_ctx=False)
        toolset.add_function(self.read_thread_history_after, takes_ctx=False)
        self.toolset = toolset

    async def for_profile(self, profile: UserProfile) -> HistoryCapability:
        """This capability bound to one person. Everyone gets one: a visitor's
        history is the threads their one account has spoken in."""
        return replace(self, profile=profile)

    @property
    def reader(self) -> UserProfile:
        if self.profile is None:
            raise RuntimeError(
                "history is read as a run's user: mount the copy `for_profile` gives"
            )
        return self.profile

    async def anchor(self, handle: str) -> ThreadMessage:
        """The message a paging tool is anchored on, with a refusal spoken as a
        retry: a handle outside this person's history is not theirs to page."""
        try:
            return await self.thread_manager.chat_message(self.reader, handle)
        except ValueError as refusal:
            raise ModelRetry(str(refusal)) from refusal

    async def search_thread_history(
        self,
        query: str,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
    ) -> list[ThreadMessage]:
        """Find visible thread messages whose text contains `query`
        (case-insensitive), across every thread the person you are answering has
        spoken in — this one, their direct messages, other chats, on any of their
        linked accounts. Optionally restrict to an actor kind such as "human",
        "agent", "bot", or "system". Oldest first."""
        return await self.thread_manager.search_chat_messages(
            self.reader, query, actor_kind=actor_kind, limit=limit
        )

    async def read_thread_history_before(
        self, message_id: str, limit: int = 10
    ) -> list[ThreadMessage]:
        """The visible thread messages immediately preceding `message_id` in its
        thread, oldest first. `message_id` is a row id or a `#msg:<id>` handle, as
        a search hit or a brief shows it; a message outside this person's history
        is refused."""
        anchor = await self.anchor(message_id)
        return await self.thread_manager.chat_messages_before(
            anchor.thread_id, anchor.id, limit=limit
        )

    async def read_thread_history_after(
        self, message_id: str, limit: int = 10
    ) -> list[ThreadMessage]:
        """The visible thread messages immediately following `message_id` in its
        thread, oldest first. `message_id` is a row id or a `#msg:<id>` handle, as
        a search hit or a brief shows it; a message outside this person's history
        is refused."""
        anchor = await self.anchor(message_id)
        return await self.thread_manager.chat_messages_after(
            anchor.thread_id, anchor.id, limit=limit
        )

    def get_toolset(self) -> FunctionToolset[Any] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return HISTORY_INSTRUCTIONS
