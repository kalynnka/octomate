from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Sequence
from typing import Literal

from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import ToolCallPart

from octomate.database import async_session
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
    ConversationKey,
)
from octomate.schemas.messages import ModelMessage, ModelResponse
from octomate.schemas.runs import AgentRun


class ConversationManager:
    """Resolves and persists `Conversation` entities, and owns their message
    history.

    The cache is the `Conversation` schema object itself — its `messages`
    relation (an arcanus list) is the in-memory history. The database is the
    source of truth; writes keep the cached transmuter object coherent after the
    commit. History is keyed by `ConversationKey` (a `ChannelAddress` + the
    owning agent), the isolation boundary — a sub-thread reception, or a
    different agent at the same address, gets its own conversation and never
    inherits another's.
    """

    def __init__(self, *, cache_size: int = 256) -> None:
        self.cache_size = cache_size
        self.conversations: OrderedDict[ConversationKey, Conversation] = OrderedDict()

    async def ensure(
        self,
        address: ChannelAddress,
        *,
        agent_tentacle_id: str,
        thread_id: uuid.UUID | None = None,
    ) -> Conversation:
        """Resolve the conversation for `(address, agent_tentacle_id)`, creating
        it if it does not yet exist. Its `conversation.messages` is the live
        history. The owning agent is part of the key — two agents at the same
        address keep separate conversations — so callers must always supply it."""
        cache_key = ConversationKey(address, agent_tentacle_id)
        cached = self.conversations.get(cache_key)
        if cached is not None:
            if thread_id is not None:
                if cached.thread_id is None:
                    cached.thread_id = thread_id
                    async with async_session() as session:
                        await session.merge(cached)
                        await session.commit()
                elif cached.thread_id != thread_id:
                    raise ValueError(
                        "conversation is already attached to a different thread"
                    )
            self.conversations.move_to_end(cache_key)
            return cached

        async with async_session() as session:
            conversation = await session.one_or_none(
                Conversation,
                expressions=[
                    Conversation["channel_tentacle_id"] == address.channel_tentacle_id,
                    Conversation["chat_type"] == address.chat_type,
                    Conversation["chat_id"] == address.chat_id,
                    Conversation["user_id"] == address.user_id,
                    Conversation["channel_thread_id"] == address.thread_id,
                    Conversation["agent_tentacle_id"] == agent_tentacle_id,
                ],
            )
            if conversation is None:
                conversation = Conversation(
                    channel_tentacle_id=address.channel_tentacle_id,
                    chat_type=address.chat_type,
                    chat_id=address.chat_id,
                    user_id=address.user_id,
                    thread_id=thread_id,
                    channel_thread_id=address.thread_id,
                    agent_tentacle_id=agent_tentacle_id,
                )
                session.add(conversation)
            elif thread_id is not None:
                if conversation.thread_id is None:
                    conversation.thread_id = thread_id
                elif conversation.thread_id != thread_id:
                    raise ValueError(
                        "conversation is already attached to a different thread"
                    )
            await session.flush()
            await conversation.runs
            await conversation.messages
            await session.commit()

        self.cache_conversation(conversation)
        return conversation

    def cache_conversation(self, conversation: Conversation) -> None:
        cache_key = ConversationKey(
            conversation.address, conversation.agent_tentacle_id
        )
        self.conversations[cache_key] = conversation
        self.conversations.move_to_end(cache_key)
        while len(self.conversations) > self.cache_size:
            self.conversations.popitem(last=False)

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
        *,
        name: str | None = None,
        external_id: str | None = None,
    ) -> AgentRun | None:
        """Persist a fresh agent run and keep the cached conversation in sync.
        `external_id`, when given, updates the conversation's resumable agent
        session handle in the same commit (external-runtime agents own their own
        session)."""
        if not messages:
            return None
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
            conversation.runs.append(run)
            conversation.external_id = external_id
            conversation = await session.merge(conversation)
            await session.commit()
            await session.refresh(conversation)

        self.cache_conversation(conversation)
        return run

    async def grant_session_tool(
        self,
        conversation: Conversation,
        tool_name: str,
    ) -> None:
        """Persist an `allow for session` grant on the conversation, so the tool
        auto-approves on later turns."""
        cached = await self.ensure(
            conversation.address, agent_tentacle_id=conversation.agent_tentacle_id
        )
        if tool_name in cached.allowed_tools:
            return
        async with async_session() as session:
            stored = await session.get(Conversation, cached.id)
            if stored is None:
                return
            stored.allowed_tools = [*stored.allowed_tools, tool_name]
            await session.commit()
        cached.allowed_tools = [*cached.allowed_tools, tool_name]
        self.cache_conversation(cached)

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
        conversation.messages.remove(last)
        for run in conversation.runs:
            if last in run.messages:
                run.messages.remove(last)
        self.cache_conversation(conversation)
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
