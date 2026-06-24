from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models import channel as channel_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ChannelAddress, ChatType, UserProfile
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.segments import MessageSegment

ChannelThreadStatus = Literal["active", "closed"]
ChannelMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "prompt_source",
    "assistant_reply",
    "assistant_send",
]


@dataclass(frozen=True)
class ChannelThreadKey:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    thread_id: str = ""

    @classmethod
    def from_address(cls, address: ChannelAddress) -> ChannelThreadKey:
        return cls(
            channel_tentacle_id=address.channel_tentacle_id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            thread_id=address.thread_id,
        )

    def __str__(self) -> str:
        return (
            f"{self.channel_tentacle_id}/{self.chat_type}/"
            f"{self.chat_id}/{self.thread_id or '-'}"
        )


@sqlalchemy_materia.bless(channel_models.ChannelMessage)
class ChannelMessage(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    channel_thread_id: uuid.UUID
    platform_message_id: str | None = None
    reply_id: str = ""
    timestamp: datetime | None = None
    direction: ChannelMessageDirection
    actor_kind: ChannelActorKind
    user_id: str = ""
    agent_tentacle_id: str | None = None
    sender: UserProfile = Field(default_factory=UserProfile)
    segments: list[MessageSegment] = Field(default_factory=list)
    message_text: str | None = None
    raw: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()


@sqlalchemy_materia.bless(channel_models.MessageBinding)
class MessageBinding(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    channel_message_id: Annotated[uuid.UUID, Identity]
    model_message_id: Annotated[uuid.UUID, Identity]
    kind: Annotated[MessageBindingKind, Identity]
    run_id: str
    tool_call_id: str | None = None
    position: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@sqlalchemy_materia.bless(channel_models.ChannelHandoff)
class ChannelHandoff(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    channel_thread_id: uuid.UUID
    from_agent_tentacle_id: str | None = None
    to_agent_tentacle_id: str
    to_model: str | None = None
    reason: str = ""
    hint: str = ""
    brief: str = ""
    source_conversation_id: uuid.UUID | None = None
    target_conversation_id: uuid.UUID | None = None
    source_run_id: str | None = None
    source_model_message_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def __lt__(self, other: ChannelHandoff) -> bool:
        return self.id < other.id

    def __gt__(self, other: ChannelHandoff) -> bool:
        return self.id > other.id


@sqlalchemy_materia.bless(channel_models.ChannelThread)
class ChannelThread(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    thread_id: str = ""
    prompt_cursor_message_id: uuid.UUID | None = None
    status: ChannelThreadStatus = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    messages: RelationCollection[ChannelMessage] = Relationships()
    handoffs: RelationCollection[ChannelHandoff] = Relationships()

    @property
    def key(self) -> ChannelThreadKey:
        return ChannelThreadKey(
            channel_tentacle_id=self.channel_tentacle_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
        )

    @property
    def latest_handoff(self) -> ChannelHandoff | None:
        return max(self.handoffs, default=None)

    @property
    def active_agent_tentacle_id(self) -> str | None:
        handoff = self.latest_handoff
        if handoff is None:
            return None
        return handoff.to_agent_tentacle_id

    @property
    def active_model(self) -> str | None:
        handoff = self.latest_handoff
        if handoff is None:
            return None
        return handoff.to_model
