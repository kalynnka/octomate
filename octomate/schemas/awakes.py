from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import TypeAlias

from pydantic import BaseModel, Field
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent


@dataclass(frozen=True)
class UserMessageSignal:
    messages: list[MessageEvent]

    def __bool__(self) -> bool:
        return bool(self.messages)

    @cached_property
    def key(self) -> ConversationKey:
        if not self.messages:
            raise ValueError("empty user message signal has no conversation key")
        last_event = self.messages[-1]
        return ConversationKey(
            channel_tentacle_id=last_event.tentacle_id,
            chat_type=last_event.chat_type,
            chat_id=last_event.chat_id,
            user_id=last_event.user_id,
            thread_id=last_event.thread_id,
        )


class DeferredActionBatchResponse(BaseModel):
    batch_id: uuid.UUID
    responder_id: str = ""
    answers: dict[uuid.UUID, str] = Field(default_factory=dict)
    approvals: dict[uuid.UUID, bool] = Field(default_factory=dict)
    allow_session: bool = False


AwakeSignal: TypeAlias = UserMessageSignal | DeferredActionBatchResponse
