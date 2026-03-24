from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from arcanus.materia.sqlalchemy import AsyncSession
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from uuid_utils import uuid7

from octomate.database import engine
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import ReplySegment
from octomate.schemas.session import SessionKey
from octomate.transmuters.messages import Message

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import ChannelTentacle

logger = logging.getLogger(__name__)


class OctopusMemory:
    max_messages: int
    history_size: int

    async def record(self, key: SessionKey, events: list[MessageEvent]) -> None:
        reply_ids = [ev.reply_id for ev in events if ev.reply_id]

        replied_map: dict[str, Message] = {}
        if reply_ids:
            async with AsyncSession(engine()) as session:
                results = await session.list(
                    Message,
                    expressions=[Message["message_id"].in_(reply_ids)],
                )
                replied_map = {m.message_id: m for m in results}

        records: list[Message] = []
        for event in events:
            if event.reply_id:
                replied = replied_map.get(event.reply_id)
                if replied:
                    for seg in event.segments:
                        if isinstance(seg, ReplySegment):
                            seg.data["content"] = replied.content
                            break
            records.append(
                Message(
                    id=str(uuid7()),
                    message_id=event.message_id,
                    reply_id=event.reply_id or None,
                    tentacle_id=key.tentacle_id,
                    thread_id=key.thread_id,
                    user=key.user_id,
                    chat=key.group_id or key.user_id,
                    timestamp=datetime.fromtimestamp(event.timestamp),
                    role="user",
                    content=str(event),
                )
            )

        if records:
            async with AsyncSession(engine()) as session:
                session.add_all(records)
                await session.commit()

    async def history(
        self, key: SessionKey, size: int | None = None
    ) -> list[ModelMessage]:
        expressions = [
            Message["tentacle_id"] == key.tentacle_id,
            Message["chat"] == (key.group_id or key.user_id),
        ]
        if key.thread_id is not None:
            expressions.append(Message["thread_id"] == key.thread_id)
        else:
            expressions.append(Message["thread_id"].is_(None))

        async with AsyncSession(engine()) as session:
            messages = await session.list(
                Message,
                order_bys=[Message["id"].desc()],
                limit=size,
            )

        model_messages: list[ModelMessage] = []
        for row in list(messages)[::-1]:
            if row.role == "user":
                model_messages.append(
                    ModelRequest(parts=[UserPromptPart(content=row.content)])
                )
            elif row.role == "assistant":
                model_messages.append(
                    ModelResponse(parts=[TextPart(content=row.content)])
                )
        return model_messages

    async def recall(
        self,
        key: SessionKey,
        events: list[MessageEvent],
        tentacle: ChannelTentacle,
        limit: int = 5,
    ) -> list[str]:
        await self.memo(key, events, tentacle)
        return []

    async def memo(
        self,
        key: SessionKey,
        messages: list[AgentMessage] | list[MessageEvent],
        tentacle: ChannelTentacle,
    ) -> None:
        records: list[Message] = []
        for msg in messages:
            if isinstance(msg, AgentMessage):
                content = "\n".join(str(seg) for seg in msg.segments)
                records.append(
                    Message(
                        id=str(uuid7()),
                        message_id="",
                        tentacle_id=key.tentacle_id,
                        thread_id=key.thread_id,
                        user=key.user_id,
                        chat=key.group_id or key.user_id,
                        timestamp=datetime.now(),
                        role="assistant",
                        content=content,
                    )
                )

        if records:
            async with AsyncSession(engine()) as session:
                session.add_all(records)
                await session.commit()
