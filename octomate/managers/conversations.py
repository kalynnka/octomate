from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Sequence
from typing import ClassVar, Literal

from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import ToolCallPart

from octomate.database import async_session
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.messages import ModelMessage, ModelResponse
from octomate.schemas.runs import AgentRun


class ConversationManager:
    """Resolves and persists `Conversation` entities, and owns their message
    history.

    The cache is the `Conversation` schema object itself — its `messages`
    relation (an arcanus list) is the in-memory history. The database is the
    source of truth: every write (`record_agent_run`, `drop_trailing_deferral`)
    commits and then `refresh`es the conversation, re-caching the freshly loaded
    object (its `runs` and `messages` come along via selectin) instead of
    patching the in-memory collections. History is keyed by `ConversationKey`
    (channel + chat + thread + user), which is the isolation boundary — a
    sub-thread reception has its own key and never inherits the main triage
    thread.
    """

    DEFAULT_CACHE_SIZE: ClassVar[int] = 256

    def __init__(self, *, cache_size: int = DEFAULT_CACHE_SIZE) -> None:
        self.cache_size = cache_size
        self.conversations: OrderedDict[ConversationKey, Conversation] = OrderedDict()

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str,
    ) -> Conversation:
        """Resolve the conversation, creating it with `agent_tentacle_id` if it
        does not yet exist. Its `conversation.messages` is the live history. The
        owning agent is set at creation and never changes, so callers must
        always supply it."""
        cached = self.conversations.get(key)
        if cached is not None:
            self.conversations.move_to_end(key)
            return cached

        async with async_session() as session:
            conversation = await session.one_or_none(
                Conversation,
                expressions=[
                    Conversation["channel_tentacle_id"] == key.channel_tentacle_id,
                    Conversation["chat_type"] == key.chat_type,
                    Conversation["chat_id"] == key.chat_id,
                    Conversation["user_id"] == key.user_id,
                    Conversation["thread_id"] == key.thread_id,
                ],
            )
            if conversation is None:
                conversation = Conversation(
                    channel_tentacle_id=key.channel_tentacle_id,
                    chat_type=key.chat_type,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    thread_id=key.thread_id,
                    agent_tentacle_id=agent_tentacle_id,
                )
                session.add(conversation)
                await session.commit()
            # Force-load the relations while the session is open so the cached
            # object can serve (and be mutated) detached, with no further I/O.
            await conversation.runs
            await conversation.messages

        self.cache_conversation(conversation)
        return conversation

    def cache_conversation(self, conversation: Conversation) -> None:
        self.conversations[conversation.key] = conversation
        self.conversations.move_to_end(conversation.key)
        while len(self.conversations) > self.cache_size:
            self.conversations.popitem(last=False)

    async def refresh(self, conversation: Conversation) -> None:
        """Reload the conversation from the database and re-cache it. The
        relations don't auto-refresh across sessions, so every write re-reads
        the persisted rows (the source of truth); `runs` and `messages` are
        eagerly (selectin) loaded, so the re-fetched object is complete."""
        async with async_session() as session:
            stored = await session.one_or_none(
                Conversation,
                expressions=[Conversation["id"] == conversation.id],
            )
            if stored is None:
                return
            await stored.runs
            await stored.messages
        self.cache_conversation(stored)

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
        *,
        name: str | None = None,
        external_id: str | None = None,
    ) -> None:
        """Persist a fresh agent run, then refresh the cached conversation from
        the database so it includes the new run. `external_id`, when
        given, updates the conversation's resumable agent session handle in the
        same commit (external-runtime agents own their own session)."""
        if not messages:
            return
        # Shallow `vars(m)` per message lets pydantic route each dict through
        # the blessed `ModelRequest | ModelResponse` discriminated union;
        # passing the raw pydantic-ai dataclass instance directly is rejected
        # since it isn't an instance of our blessed subclass.
        run = AgentRun(
            id=run_id,
            conversation_id=conversation.id,
            name=name,
            started_at=messages[0].timestamp,
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
        )
        async with async_session() as session:
            session.add(run)
            if external_id is not None:
                stored = await session.get(Conversation, conversation.id)
                if stored is not None:
                    stored.external_id = external_id
            await session.commit()
        await self.refresh(conversation)

    async def drop_trailing_deferral(
        self,
        conversation: Conversation,
    ) -> ModelResponse | None:
        """If the conversation's last message is an abandoned deferred-tool
        ModelResponse (a tool-call request a new user turn supersedes), delete
        it from the database, refresh the cached conversation, and return it.
        Without the delete the orphan would resurface mid-history on a cold
        reload, where it can no longer be recognized as a trailing deferral.
        """
        messages = conversation.messages
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, ModelResponse):
            return None
        if not any(isinstance(part, ToolCallPart) for part in last.parts):
            return None
        async with async_session() as session:
            await session.delete(last)
            await session.commit()
        await self.refresh(conversation)
        return last

    async def search_messages(
        self,
        conversation_id: uuid.UUID,
        query: str,
        *,
        role: Literal["user", "assistant"] | None = None,
        limit: int = 10,
    ) -> list[ModelMessage]:
        """Conversation messages whose `message_text` contains `query`
        (case-insensitive), ordered chronologically. One polymorphic select spans
        both kinds, so an unfiltered search returns user and assistant hits in a
        single pass; `role` narrows it when set. Tool-call/thinking messages carry
        no `message_text` and never match — reach them via
        `messages_before`/`messages_after`."""
        expressions = [
            ModelMessage["conversation_id"] == str(conversation_id),
            ModelMessage["message_text"].ilike(f"%{query}%"),
        ]
        if role is not None:
            expressions.append(ModelMessage["role"] == role)
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def messages_before(
        self,
        conversation_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ModelMessage]:
        """The `limit` messages immediately preceding `anchor_id` in the
        conversation (all kinds), oldest-first."""
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"].desc()],
                expressions=[
                    ModelMessage["conversation_id"] == str(conversation_id),
                    ModelMessage["id"] < anchor_id,
                ],
            )
        return list(reversed(rows))

    async def messages_after(
        self,
        conversation_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ModelMessage]:
        """The `limit` messages immediately following `anchor_id` in the
        conversation (all kinds), oldest-first."""
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"]],
                expressions=[
                    ModelMessage["conversation_id"] == str(conversation_id),
                    ModelMessage["id"] > anchor_id,
                ],
            )
        return list(rows)
