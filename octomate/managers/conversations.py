from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage

from octomate.database import async_session
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.messages import ModelResponse
from octomate.schemas.runs import AgentRun


class ConversationManager:
    """Resolves and persists `Conversation` entities directly against the DB."""

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> Conversation:
        async with async_session() as session:
            existing = await session.one_or_none(
                Conversation,
                expressions=[
                    Conversation["channel_tentacle_id"] == key.channel_tentacle_id,
                    Conversation["chat_type"] == key.chat_type,
                    Conversation["chat_id"] == key.chat_id,
                    Conversation["user_id"] == key.user_id,
                    Conversation["thread_id"] == key.thread_id,
                ],
            )
            if existing is not None:
                if existing.agent_tentacle_id is None and agent_tentacle_id is not None:
                    existing.agent_tentacle_id = agent_tentacle_id
                    await session.commit()
                await existing.runs
                await existing.messages
                return existing

            created = Conversation(
                channel_tentacle_id=key.channel_tentacle_id,
                chat_type=key.chat_type,
                chat_id=key.chat_id,
                user_id=key.user_id,
                thread_id=key.thread_id,
                agent_tentacle_id=agent_tentacle_id,
            )
            session.add(created)
            await session.commit()
            await created.runs
            await created.messages
            return created

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[ModelMessage],
    ) -> None:
        """Persist a fresh agent run with the raw pydantic-ai messages it
        produced. Blessing happens here so callers stay clean of the
        `ModelMessageListAdapter` / `AgentRun` schema imports.
        """
        if not messages:
            return
        # Shallow `vars(m)` per message lets pydantic route each dict through
        # the blessed `ModelRequest | ModelResponse` discriminated union;
        # passing the raw pydantic-ai dataclass instance directly is rejected
        # since it isn't an instance of our blessed subclass.
        run = AgentRun(
            id=run_id,
            conversation_id=conversation.id,
            started_at=messages[0].timestamp,
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
        )
        async with async_session() as session:
            session.add(run)
            await session.commit()

    async def discard_message(self, message: ModelResponse) -> None:
        """Delete a single persisted model response message from the DB.

        Used to drop an abandoned deferred-tool ModelResponse when a new
        user turn supersedes it — without this, the orphaned response
        would reappear in the middle of history on the next reload (where
        `drop_trailing_deferral` can't catch it, since it only looks at
        the tail).
        """
        async with async_session() as session:
            await session.delete(message)
            await session.commit()
