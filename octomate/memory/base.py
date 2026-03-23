from __future__ import annotations

import logging
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
from octomate.schemas.session import SessionKey
from octomate.transmuters.messages import Message

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)


class OctopusMemory:
    max_messages: int
    history_size: int

    async def record(self, key: SessionKey, messages: list[ModelMessage]) -> None:
        records: list[Message] = []

        for msg in messages:
            if isinstance(msg, ModelRequest):
                parts = [p for p in msg.parts if isinstance(p, UserPromptPart)]
                for part in parts:
                    content = (
                        part.content
                        if isinstance(part.content, str)
                        else str(part.content)
                    )
                    records.append(
                        Message(
                            id=str(uuid7()),
                            tentacle_id=key.tentacle_id,
                            thread_id=key.thread_id,
                            user=key.user_id,
                            chat=key.group_id or key.user_id,
                            timestamp=part.timestamp,
                            role="user",
                            content=content,
                        )
                    )
            elif isinstance(msg, ModelResponse):
                parts = [p for p in msg.parts if isinstance(p, TextPart)]
                for part in parts:
                    records.append(
                        Message(
                            id=str(uuid7()),
                            tentacle_id=key.tentacle_id,
                            thread_id=key.thread_id,
                            user=key.user_id,
                            chat=key.group_id or key.user_id,
                            timestamp=msg.timestamp,
                            role="assistant",
                            content=part.content,
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
            Message["chat"] == key.group_id or key.user_id,
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
        tentacle: Tentacle,
        limit: int = 5,
    ) -> list[str]:
        await self.memo(key, events, tentacle)
        return []

    async def memo(
        self,
        key: SessionKey,
        messages: list[AgentMessage] | list[MessageEvent],
        tentacle: Tentacle,
    ) -> None:
        pass
