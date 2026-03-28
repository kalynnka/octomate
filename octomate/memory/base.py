from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import ReplySegment
from octomate.schemas.session import SessionKey
from octomate.stores.message import MessageStore
from octomate.transmuters.messages import Message

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import ChannelTentacle

logger = logging.getLogger(__name__)


class OctopusMemory:
    max_messages: int
    history_size: int
    messages: MessageStore

    def __init__(self) -> None:
        self.messages = MessageStore()

    async def record(self, key: SessionKey, events: list[MessageEvent]) -> None:
        reply_ids = [ev.reply_id for ev in events if ev.reply_id]
        replied_map = await self.messages.find_by_ids(reply_ids)

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

        await self.messages.save_all(records)

    async def history(
        self, key: SessionKey, size: int | None = None
    ) -> list[ModelMessage]:
        recent = await self.messages.list_recent(key, size)

        model_messages: list[ModelMessage] = []
        for row in list(recent)[::-1]:
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

        await self.messages.save_all(records)
