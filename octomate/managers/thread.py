from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import or_

from octomate.config.agents import AgentRouteModelName
from octomate.database import async_session
from octomate.schemas.channel import (
    ChannelActorKind,
    Handoff,
    ThreadMessage,
    Thread,
    ThreadKey,
    MessageBinding,
    MessageBindingKind,
)
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelResponse
from octomate.schemas.segments import MarkdownSegment, MessageSegment, TextSegment


def message_text_from_segments(segments: list[MessageSegment]) -> str | None:
    parts = [
        segment.data["text"]
        for segment in segments
        if isinstance(segment, (TextSegment, MarkdownSegment))
    ]
    if not parts:
        return None
    return "\n".join(parts)


class ThreadManager:
    """Owns durable thread chat ledger persistence.

    The cache mirrors `ConversationManager`: the database is the source of truth,
    and write methods keep the cached `Thread` coherent after commits.
    """

    def __init__(self, *, cache_size: int = 256) -> None:
        self.cache_size = cache_size
        self.threads: OrderedDict[ThreadKey, Thread] = OrderedDict()

    async def ensure_thread(
        self,
        address_or_key: ChannelAddress | ThreadKey,
    ) -> Thread:
        if isinstance(address_or_key, ChannelAddress):
            key = ThreadKey.from_address(address_or_key)
        else:
            key = address_or_key
        cached = self.threads.get(key)
        if cached is not None:
            self.threads.move_to_end(key)
            return cached

        async with async_session() as session:
            thread = await session.one_or_none(
                Thread,
                expressions=[
                    Thread["channel_tentacle_id"] == key.channel_tentacle_id,
                    Thread["chat_type"] == key.chat_type,
                    Thread["chat_id"] == key.chat_id,
                    Thread["thread_id"] == key.thread_id,
                ],
            )
            if thread is None:
                thread = Thread(
                    channel_tentacle_id=key.channel_tentacle_id,
                    chat_type=key.chat_type,
                    chat_id=key.chat_id,
                    thread_id=key.thread_id,
                )
                session.add(thread)
            await session.flush()
            await thread.messages
            await thread.handoffs
            await session.commit()

        self.cache_thread(thread)
        return thread

    def cache_thread(self, thread: Thread) -> None:
        self.threads[thread.key] = thread
        self.threads.move_to_end(thread.key)
        while len(self.threads) > self.cache_size:
            self.threads.popitem(last=False)

    async def record_inbound(
        self,
        event: MessageEvent,
        *,
        actor_kind: ChannelActorKind = "human",
        agent_tentacle_id: str | None = None,
    ) -> ThreadMessage:
        thread = await self.ensure_thread(
            ThreadKey(
                channel_tentacle_id=event.tentacle_id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                thread_id=event.thread_id,
            )
        )
        message = ThreadMessage(
            thread_id=thread.id,
            platform_message_id=event.message_id or None,
            reply_id=event.reply_id,
            timestamp=(
                datetime.fromtimestamp(event.timestamp, timezone.utc)
                if event.timestamp > 0
                else None
            ),
            direction="inbound",
            actor_kind=actor_kind,
            user_id=event.user_id,
            agent_tentacle_id=agent_tentacle_id,
            sender=event.sender,
            segments=event.segments,
            message_text=message_text_from_segments(event.segments),
            raw=event.raw,
        )
        async with async_session() as session:
            session.add(message)
            await session.commit()
        thread.messages.append(message)
        self.cache_thread(thread)
        return message

    async def record_outbound(
        self,
        thread_or_address: Thread | ChannelAddress | ThreadKey,
        *,
        agent_tentacle_id: str,
        segments: list[MessageSegment],
        platform_message_id: str | None = None,
        reply_id: str = "",
        timestamp: datetime | None = None,
        sender: UserProfile | None = None,
        actor_kind: ChannelActorKind = "agent",
        message_text: str | None = None,
        raw: str = "",
    ) -> ThreadMessage:
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        message = ThreadMessage(
            thread_id=thread.id,
            platform_message_id=platform_message_id,
            reply_id=reply_id,
            timestamp=timestamp,
            direction="outbound",
            actor_kind=actor_kind,
            user_id="",
            agent_tentacle_id=agent_tentacle_id,
            sender=sender
            or UserProfile(user_id=agent_tentacle_id, name=agent_tentacle_id),
            segments=segments,
            message_text=message_text or message_text_from_segments(segments),
            raw=raw,
        )
        async with async_session() as session:
            session.add(message)
            await session.commit()
        thread.messages.append(message)
        self.cache_thread(thread)
        return message

    async def mark_presented(
        self,
        message: ThreadMessage,
        platform_message_id: str | None,
    ) -> ThreadMessage:
        if platform_message_id is None:
            return message
        message.platform_message_id = platform_message_id
        async with async_session() as session:
            stored = await session.get(ThreadMessage, message.id)
            if stored is None:
                raise ValueError(f"thread message {message.id} does not exist")
            stored.platform_message_id = platform_message_id
            await session.commit()
        return message

    async def pending_prompt_messages(
        self,
        thread: Thread,
        trigger_message_id: uuid.UUID,
        active_agent_id: str,
    ) -> list[ThreadMessage]:
        cached = await self.ensure_thread(thread.key)

        async with async_session() as session:
            expressions = [
                ThreadMessage["thread_id"] == cached.id,
                ThreadMessage["id"] <= trigger_message_id,
                or_(
                    ThreadMessage["actor_kind"] != "agent",
                    ThreadMessage["agent_tentacle_id"] != active_agent_id,
                    ThreadMessage["agent_tentacle_id"].is_(None),
                ),
            ]
            if cached.source_cursor_message_id is not None:
                expressions.append(
                    ThreadMessage["id"] > cached.source_cursor_message_id
                )
            rows = await session.list(
                ThreadMessage,
                limit=None,
                order_bys=[ThreadMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def advance_prompt_cursor(
        self,
        thread: Thread,
        message_id: uuid.UUID,
    ) -> Thread | None:
        async with async_session() as session:
            stored = await session.get(Thread, thread.id)
            if stored is None:
                return None
            stored.source_cursor_message_id = message_id
            await session.commit()
        thread.source_cursor_message_id = message_id
        self.cache_thread(thread)
        return thread

    async def record_handoff(
        self,
        thread_or_address: Thread | ChannelAddress | ThreadKey,
        *,
        to_agent_tentacle_id: str,
        from_agent_tentacle_id: str | None = None,
        to_model: AgentRouteModelName | None = None,
        reason: str = "",
        hint: str = "",
        brief: str = "",
        source_conversation_id: uuid.UUID | None = None,
        target_conversation_id: uuid.UUID | None = None,
        source_run_id: str | None = None,
        source_model_message_id: uuid.UUID | None = None,
    ) -> Handoff:
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        handoff = Handoff(
            thread_id=thread.id,
            from_agent_tentacle_id=from_agent_tentacle_id,
            to_agent_tentacle_id=to_agent_tentacle_id,
            to_model=to_model,
            reason=reason,
            hint=hint,
            brief=brief,
            source_conversation_id=source_conversation_id,
            target_conversation_id=target_conversation_id,
            source_run_id=source_run_id,
            source_model_message_id=source_model_message_id,
        )
        async with async_session() as session:
            session.add(handoff)
            await session.commit()
        thread.handoffs.append(handoff)
        self.cache_thread(thread)
        return handoff

    async def bind_messages(
        self,
        thread_message_ids: list[uuid.UUID],
        model_message_id: uuid.UUID,
        *,
        kind: MessageBindingKind,
        run_id: str,
        tool_call_id: str | None = None,
    ) -> list[MessageBinding]:
        bindings: list[MessageBinding] = []
        async with async_session() as session:
            for position, thread_message_id in enumerate(thread_message_ids):
                binding = MessageBinding(
                    thread_message_id=thread_message_id,
                    model_message_id=model_message_id,
                    kind=kind,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    position=position,
                )
                session.add(binding)
                bindings.append(binding)
            await session.commit()
        return bindings

    async def bind_assistant_replies(
        self,
        thread_message_ids: list[uuid.UUID],
        *,
        run_id: str,
    ) -> list[MessageBinding]:
        if not thread_message_ids:
            return []
        async with async_session() as session:
            responses = await session.list(
                ModelResponse,
                limit=None,
                order_bys=[ModelResponse["id"]],
                expressions=[
                    ModelResponse["run_id"] == run_id,
                    ModelResponse["role"] == "assistant",
                    ModelResponse["message_text"].is_not(None),
                    ModelResponse["message_text"] != "",
                ],
            )
        assistant_replies = list(responses)
        if not assistant_replies:
            return []
        if len(assistant_replies) == len(thread_message_ids):
            bindings: list[MessageBinding] = []
            async with async_session() as session:
                for thread_message_id, response in zip(
                    thread_message_ids, assistant_replies, strict=True
                ):
                    binding = MessageBinding(
                        thread_message_id=thread_message_id,
                        model_message_id=response.id,
                        kind="assistant_reply",
                        run_id=run_id,
                        position=0,
                    )
                    session.add(binding)
                    bindings.append(binding)
                await session.commit()
            return bindings
        return await self.bind_messages(
            thread_message_ids,
            assistant_replies[-1].id,
            kind="assistant_reply",
            run_id=run_id,
        )

    async def search_chat_messages(
        self,
        thread_id: uuid.UUID,
        query: str,
        *,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
    ) -> list[ThreadMessage]:
        expressions = [
            ThreadMessage["thread_id"] == thread_id,
            ThreadMessage["message_text"].ilike(f"%{query}%"),
        ]
        if actor_kind is not None:
            expressions.append(ThreadMessage["actor_kind"] == actor_kind)
        async with async_session() as session:
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[ThreadMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def chat_messages_before(
        self,
        thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ThreadMessage]:
        async with async_session() as session:
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[ThreadMessage["id"].desc()],
                expressions=[
                    ThreadMessage["thread_id"] == thread_id,
                    ThreadMessage["id"] < anchor_id,
                ],
            )
        return list(reversed(rows))

    async def chat_messages_after(
        self,
        thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ThreadMessage]:
        async with async_session() as session:
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[ThreadMessage["id"]],
                expressions=[
                    ThreadMessage["thread_id"] == thread_id,
                    ThreadMessage["id"] > anchor_id,
                ],
            )
        return list(rows)
