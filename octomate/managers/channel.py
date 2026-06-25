from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import or_

from octomate.database import async_session
from octomate.schemas.channel import (
    ChannelActorKind,
    ChannelHandoff,
    ChannelMessage,
    ChannelThread,
    ChannelThreadKey,
    MessageBinding,
    MessageBindingKind,
)
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.events import MessageEvent
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


class ChannelThreadManager:
    """Owns durable channel-thread chat ledger persistence.

    The cache mirrors `ConversationManager`: the database is the source of truth,
    and write methods refresh the cached `ChannelThread` so hot callers see the
    latest cursor, handoff, and message relations.
    """

    def __init__(self, *, cache_size: int = 256) -> None:
        self.cache_size = cache_size
        self.threads: OrderedDict[ChannelThreadKey, ChannelThread] = OrderedDict()

    async def ensure_thread(
        self,
        address_or_key: ChannelAddress | ChannelThreadKey,
    ) -> ChannelThread:
        if isinstance(address_or_key, ChannelAddress):
            key = ChannelThreadKey.from_address(address_or_key)
        else:
            key = address_or_key
        cached = self.threads.get(key)
        if cached is not None:
            self.threads.move_to_end(key)
            return cached

        async with async_session() as session:
            thread = await session.one_or_none(
                ChannelThread,
                expressions=[
                    ChannelThread["channel_tentacle_id"] == key.channel_tentacle_id,
                    ChannelThread["chat_type"] == key.chat_type,
                    ChannelThread["chat_id"] == key.chat_id,
                    ChannelThread["thread_id"] == key.thread_id,
                ],
            )
            if thread is None:
                thread = ChannelThread(
                    channel_tentacle_id=key.channel_tentacle_id,
                    chat_type=key.chat_type,
                    chat_id=key.chat_id,
                    thread_id=key.thread_id,
                )
                session.add(thread)
                await session.commit()
            await thread.messages
            await thread.handoffs

        self.cache_thread(thread)
        return thread

    def cache_thread(self, thread: ChannelThread) -> None:
        self.threads[thread.key] = thread
        self.threads.move_to_end(thread.key)
        while len(self.threads) > self.cache_size:
            self.threads.popitem(last=False)

    async def refresh(self, thread: ChannelThread) -> ChannelThread | None:
        async with async_session() as session:
            stored = await session.one_or_none(
                ChannelThread,
                expressions=[ChannelThread["id"] == thread.id],
            )
            if stored is None:
                return None
            await stored.messages
            await stored.handoffs
        self.cache_thread(stored)
        return stored

    async def record_inbound(
        self,
        event: MessageEvent,
        *,
        actor_kind: ChannelActorKind = "human",
        agent_tentacle_id: str | None = None,
    ) -> ChannelMessage:
        thread = await self.ensure_thread(
            ChannelThreadKey(
                channel_tentacle_id=event.tentacle_id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                thread_id=event.thread_id,
            )
        )
        message = ChannelMessage(
            channel_thread_id=thread.id,
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
        return message

    async def record_outbound(
        self,
        thread_or_address: ChannelThread | ChannelAddress | ChannelThreadKey,
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
    ) -> ChannelMessage:
        if isinstance(thread_or_address, ChannelThread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        message = ChannelMessage(
            channel_thread_id=thread.id,
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
        return message

    async def pending_prompt_messages(
        self,
        thread: ChannelThread,
        trigger_message_id: uuid.UUID,
        active_agent_id: str,
    ) -> list[ChannelMessage]:
        cached = await self.ensure_thread(thread.key)

        async with async_session() as session:
            expressions = [
                ChannelMessage["channel_thread_id"] == cached.id,
                ChannelMessage["id"] <= trigger_message_id,
                or_(
                    ChannelMessage["actor_kind"] != "agent",
                    ChannelMessage["agent_tentacle_id"] != active_agent_id,
                    ChannelMessage["agent_tentacle_id"].is_(None),
                ),
            ]
            if cached.prompt_cursor_message_id is not None:
                expressions.append(
                    ChannelMessage["id"] > cached.prompt_cursor_message_id
                )
            rows = await session.list(
                ChannelMessage,
                limit=None,
                order_bys=[ChannelMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def advance_prompt_cursor(
        self,
        thread: ChannelThread,
        message_id: uuid.UUID,
    ) -> ChannelThread | None:
        async with async_session() as session:
            stored = await session.get(ChannelThread, thread.id)
            if stored is None:
                return None
            stored.prompt_cursor_message_id = message_id
            await session.commit()
            await stored.messages
            await stored.handoffs
        self.cache_thread(stored)
        return stored

    async def record_handoff(
        self,
        thread_or_address: ChannelThread | ChannelAddress | ChannelThreadKey,
        *,
        to_agent_tentacle_id: str,
        from_agent_tentacle_id: str | None = None,
        to_model: str | None = None,
        reason: str = "",
        hint: str = "",
        brief: str = "",
        source_conversation_id: uuid.UUID | None = None,
        target_conversation_id: uuid.UUID | None = None,
        source_run_id: str | None = None,
        source_model_message_id: uuid.UUID | None = None,
    ) -> ChannelHandoff:
        if isinstance(thread_or_address, ChannelThread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        handoff = ChannelHandoff(
            channel_thread_id=thread.id,
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
        await self.refresh(thread)
        return handoff

    async def bind_messages(
        self,
        channel_message_ids: list[uuid.UUID],
        model_message_id: uuid.UUID,
        *,
        kind: MessageBindingKind,
        run_id: str,
        tool_call_id: str | None = None,
    ) -> list[MessageBinding]:
        bindings: list[MessageBinding] = []
        async with async_session() as session:
            for position, channel_message_id in enumerate(channel_message_ids):
                binding = MessageBinding(
                    channel_message_id=channel_message_id,
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

    async def search_chat_messages(
        self,
        channel_thread_id: uuid.UUID,
        query: str,
        *,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
    ) -> list[ChannelMessage]:
        expressions = [
            ChannelMessage["channel_thread_id"] == channel_thread_id,
            ChannelMessage["message_text"].ilike(f"%{query}%"),
        ]
        if actor_kind is not None:
            expressions.append(ChannelMessage["actor_kind"] == actor_kind)
        async with async_session() as session:
            rows = await session.list(
                ChannelMessage,
                limit=limit,
                order_bys=[ChannelMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def chat_messages_before(
        self,
        channel_thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ChannelMessage]:
        async with async_session() as session:
            rows = await session.list(
                ChannelMessage,
                limit=limit,
                order_bys=[ChannelMessage["id"].desc()],
                expressions=[
                    ChannelMessage["channel_thread_id"] == channel_thread_id,
                    ChannelMessage["id"] < anchor_id,
                ],
            )
        return list(reversed(rows))

    async def chat_messages_after(
        self,
        channel_thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ChannelMessage]:
        async with async_session() as session:
            rows = await session.list(
                ChannelMessage,
                limit=limit,
                order_bys=[ChannelMessage["id"]],
                expressions=[
                    ChannelMessage["channel_thread_id"] == channel_thread_id,
                    ChannelMessage["id"] > anchor_id,
                ],
            )
        return list(rows)
