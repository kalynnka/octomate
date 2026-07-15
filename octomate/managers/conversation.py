from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Sequence
from typing import Literal, TypeVar

from arcanus.materia.sqlalchemy import selectinload
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import ToolCallPart
from sqlalchemy import insert, select
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.schemas.conversation import (
    Conversation,
    ConversationKey,
)
from octomate.schemas.messages import ModelMessage, ModelResponse
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.schemas.thread import ThreadMessage

RunT = TypeVar("RunT", bound=AgentRun)


class ConversationManager:
    """Resolves and persists agent `Conversation` entities, and owns their model
    message history.

    A `Conversation` is one agent's model context within a `Thread` — not a human
    chat log; the user-facing chat ledger is `ThreadManager`'s `ThreadMessage`
    rows. A thread owns one conversation per agent, so the identity is
    `(thread_id, agent_tentacle_id)`: every sender in a group thread keys to the
    same owning agent's conversation, regardless of who woke it.

    The cache is the `Conversation` schema object itself — its `messages`
    relation (an arcanus list) is the in-memory history. The database is the
    source of truth; writes keep the cached transmuter object coherent after the
    commit.
    """

    def __init__(self, *, cache_size: int = 256) -> None:
        self.cache_size = cache_size
        self.conversations: OrderedDict[ConversationKey, Conversation] = OrderedDict()

    async def ensure(
        self,
        thread_id: uuid.UUID,
        *,
        agent_tentacle_id: str,
    ) -> Conversation:
        """Resolve the conversation owned by `agent_tentacle_id` in `thread_id`,
        creating it if it does not yet exist. Its `conversation.messages` is the
        live model history. The owning agent is part of the identity — two agents
        in the same thread keep separate conversations — so callers must always
        supply it."""
        cache_key = ConversationKey(thread_id, agent_tentacle_id)
        cached = self.conversations.get(cache_key)
        if cached is not None:
            self.conversations.move_to_end(cache_key)
            return cached

        async with async_session() as session:
            conversation = await session.one_or_none(
                Conversation,
                expressions=[
                    Conversation["thread_id"] == thread_id,
                    Conversation["agent_tentacle_id"] == agent_tentacle_id,
                ],
            )
            if conversation is None:
                conversation = Conversation(
                    thread_id=thread_id,
                    agent_tentacle_id=agent_tentacle_id,
                )
                session.add(conversation)
            await session.flush()
            await conversation.runs
            await conversation.messages
            await session.commit()

        self.cache_conversation(conversation)
        return conversation

    def cache_conversation(self, conversation: Conversation) -> None:
        cache_key = conversation.key
        self.conversations[cache_key] = conversation
        self.conversations.move_to_end(cache_key)
        while len(self.conversations) > self.cache_size:
            self.conversations.popitem(last=False)

    async def thread_id(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        for conversation in self.conversations.values():
            if conversation.id == conversation_id:
                return conversation.thread_id
        async with async_session() as session:
            conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        return conversation.thread_id

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
        run = AgentRun(
            id=run_id,
            conversation_id=conversation.id,
            name=name,
            started_at=messages[0].timestamp,
            # Shallow `vars(m)` dicts, so pydantic routes each through the blessed
            # `ModelRequest | ModelResponse` union at construction — the raw pydantic-ai
            # dataclass is rejected, not being an instance of our blessed subclass.
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
        )
        return await self.persist_run(
            run, conversation_id=conversation.id, external_id=external_id
        )

    async def record_external_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
        *,
        name: str | None = None,
        external_session_id: str,
        source: str | None = None,
        start_offset: int | None = None,
        end_offset: int | None = None,
        last_line_uuid: str | None = None,
    ) -> ExternalAgentRun | None:
        """Persist a run rebuilt from an external runtime's transcript (native Claude)
        as the `external` variant, carrying its transcript coordinates.
        `external_session_id` doubles as the conversation's resumable handle
        (`external_id`)."""
        if not messages:
            return None
        run = ExternalAgentRun(
            id=run_id,
            conversation_id=conversation.id,
            name=name,
            started_at=messages[0].timestamp,
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
            external_session_id=external_session_id,
            source=source,
            start_offset=start_offset,
            end_offset=end_offset,
            last_line_uuid=last_line_uuid,
        )
        return await self.persist_run(
            run, conversation_id=conversation.id, external_id=external_session_id
        )

    async def persist_run(
        self,
        run: RunT,
        *,
        conversation_id: uuid.UUID,
        external_id: str | None,
    ) -> RunT:
        """Persist a freshly built run and re-sync the cached conversation.
        `external_id`, when given, updates the resumable agent-session handle in the
        same commit.

        Persist only the new run and its messages, then reload the conversation to
        re-sync the cache. Re-merging the whole cached conversation graph (its prior
        runs and their message collections) can make SQLAlchemy believe a message was
        dropped from a stale run collection and null its NOT NULL `run_id`, raising an
        IntegrityError; adding just the new run sidesteps that reconciliation entirely.
        The cached `conversation` is detached, so it can't be refreshed in place — the
        freshly loaded copy becomes the coherent cache the next ensure() returns.
        """
        async with async_session() as session:
            session.add(run)
            reloaded = await session.one_or_none(
                Conversation, expressions=[Conversation["id"] == conversation_id]
            )
            if reloaded is not None:
                reloaded.external_id = external_id
            await session.commit()
            if reloaded is not None:
                await reloaded.runs
                await reloaded.messages
                self.cache_conversation(reloaded)
        return run

    async def fork(
        self,
        source: Conversation,
        target: Conversation,
    ) -> AgentRun | None:
        """Fork `source`'s full message history into `target` as one new run, so a
        same-agent `teleport` resumes seamlessly against the copy.

        The rows are copied at the database level — a bulk `INSERT` off a `SELECT` of
        the source rows, keyed to fresh, order-preserving ids — so nothing round-trips
        through Python message objects. The trailing deferred `ModelResponse` (the
        pending `teleport` call) is copied like any other row, so the resume stays
        valid. Fails fast if `target` already holds messages: copying onto an existing
        history would splice two unrelated conversations."""
        if target.messages:
            raise ValueError(
                f"fork target {target.id} already holds "
                f"{len(target.messages)} messages; refusing to splice histories"
            )
        source_ids = [message.id for message in source.messages]
        if not source_ids:
            return None

        run_id = str(uuid7())
        async with async_session() as session:
            rows = (
                (
                    await session.execute(
                        select(*select(ModelMessage).selected_columns)
                        .where(ModelMessage["id"].in_(source_ids))
                        .order_by(ModelMessage["id"])
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return None
            # New uuid7 ids generated in source order stay monotonic, so the copied
            # rows keep their ordering under the id-ordered messages relation.
            cloned = [
                {
                    **row,
                    "id": uuid7(),
                    "run_id": run_id,
                    "conversation_id": str(target.id),
                }
                for row in rows
            ]
            await session.execute(
                insert(AgentRun).values(
                    id=run_id,
                    conversation_id=target.id,
                    name="fork",
                    started_at=rows[0]["timestamp"],
                )
            )
            await session.execute(insert(ModelMessage), cloned)
            await session.commit()

            forked_run = await session.get(AgentRun, run_id)
            refreshed = await session.get(Conversation, target.id)
            if refreshed is not None:
                await refreshed.runs
                await refreshed.messages
        if refreshed is not None:
            self.cache_conversation(refreshed)
        return forked_run

    async def grant_session_tool(
        self,
        conversation: Conversation,
        tool_name: str,
    ) -> None:
        """Persist an `allow for session` grant on the conversation, so the tool
        auto-approves on later turns."""
        cached = await self.ensure(
            conversation.thread_id,
            agent_tentacle_id=conversation.agent_tentacle_id,
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

    async def related_chat_messages(
        self,
        model_message_id: uuid.UUID,
    ) -> list[ThreadMessage]:
        async with async_session() as session:
            message = await session.one_or_none(
                ModelMessage,
                options=[selectinload(ModelMessage["thread_messages"])],
                expressions=[ModelMessage["id"] == model_message_id],
            )
            if message is None:
                return []
            return [
                ThreadMessage.model_validate(thread_message)
                for thread_message in message.thread_messages
            ]
