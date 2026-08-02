from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from arcanus import BaseTransmuter, Relation, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import AfterValidator, ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.config.agents import AgentRouteModelName
from octomate.models import thread as thread_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.messages import native_utc
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile

if TYPE_CHECKING:
    from octomate.schemas.messages import ModelRequest, ModelResponse

ThreadStatus = Literal["active", "closed"]
ThreadMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "request_source",
    "assistant_reply",
    "assistant_send",
]


@dataclass(frozen=True)
class ThreadKey:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    thread_id: str = ""

    @classmethod
    def from_address(cls, address: ChannelAddress) -> ThreadKey:
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


@sqlalchemy_materia.bless(thread_models.ThreadMessage)
class ThreadMessage(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    thread_id: uuid.UUID
    platform_message_id: str | None = None
    reply_id: str = ""
    # When the message happened, and what the ledger orders on: a platform's or a
    # transcript's clock where one is known, else when Octomate recorded it. Never unset
    # — the order is only total if every row has one. `created_at` stays the bookkeeping
    # answer to "when was this written", which for replayed history is a different
    # instant entirely.
    happened_at: Annotated[datetime, AfterValidator(native_utc)] = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    direction: ThreadMessageDirection
    actor_kind: ChannelActorKind
    user_id: str = ""
    agent_tentacle_id: str | None = None
    sender_id: uuid.UUID = Field(
        description=(
            "The sender's registry profile row (user_profiles); `sender` "
            "resolves it. Inbound: the platform account; outbound: the channel "
            "bot or a native session's pseudo-user."
        ),
    )
    sender: Relation[UserProfile] = Field(default_factory=Relation, frozen=True)
    segments: list[MessageSegment] = Field(default_factory=list)
    message_text: str | None = None
    raw: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()


@sqlalchemy_materia.bless(thread_models.MessageBinding)
class MessageBinding(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    thread_message_id: Annotated[uuid.UUID, Identity]
    model_message_id: Annotated[uuid.UUID, Identity]
    kind: Annotated[MessageBindingKind, Identity]
    run_id: str
    tool_call_id: str | None = None
    position: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@sqlalchemy_materia.bless(thread_models.Handoff)
class Handoff(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    thread_id: uuid.UUID
    from_agent_tentacle_id: str | None = None
    to_agent_tentacle_id: str
    to_model: AgentRouteModelName | None = None
    reason: str = ""
    hint: str = ""
    brief: str = ""
    source_conversation_id: uuid.UUID | None = None
    target_conversation_id: uuid.UUID | None = None
    source_run_id: str | None = None
    source_model_message_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def __lt__(self, other: Handoff) -> bool:
        return self.id < other.id

    def __gt__(self, other: Handoff) -> bool:
        return self.id > other.id


@sqlalchemy_materia.bless(thread_models.Thread)
class Thread(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    thread_id: str = ""
    source_cursor_message_id: uuid.UUID | None = None
    status: ThreadStatus = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    messages: RelationCollection[ThreadMessage] = Relationships()
    handoffs: RelationCollection[Handoff] = Relationships()

    @property
    def key(self) -> ThreadKey:
        return ThreadKey(
            channel_tentacle_id=self.channel_tentacle_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
        )

    @property
    def latest_handoff(self) -> Handoff | None:
        return max(self.handoffs, default=None)

    @property
    def active_agent_tentacle_id(self) -> str | None:
        handoff = self.latest_handoff
        if handoff is None:
            return None
        return handoff.to_agent_tentacle_id

    @property
    def active_model(self) -> AgentRouteModelName | None:
        handoff = self.latest_handoff
        if handoff is None:
            return None
        return handoff.to_model


from octomate.schemas.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
)

ThreadMessage.model_rebuild(
    _types_namespace={
        "ModelRequest": ModelRequest,
        "ModelResponse": ModelResponse,
    }
)
ModelMessage.model_rebuild(_types_namespace={"ThreadMessage": ThreadMessage})
