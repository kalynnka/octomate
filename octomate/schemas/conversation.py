from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Literal, NamedTuple

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import BaseModel, ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.conversation import Conversation as ConversationModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.runs import AgentRun

ChatType = Literal["private", "group"]

# The approval level a conversation grants an external coding agent (Claude):
# prompt every gated tool ("default"), auto-accept edits, or skip all gating
# ("bypass_permissions"). Our values are snake_case; the Claude tentacle maps
# them to the SDK's camelCase `permission_mode` at the boundary.
ConversationPermissionMode = Literal["default", "accept_edits", "bypass_permissions"]


@dataclass(frozen=True)
class ChannelAddress:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    user_id: str
    # Thread id as named by the channel platform/address, not `Thread.id`.
    thread_id: str = ""

    @cached_property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @cached_property
    def group_id(self) -> str:
        return self.chat_id if self.chat_type == "group" else ""

    @cached_property
    def topic_memory_key(self) -> str:
        return f"topic:{self.channel_tentacle_id}:{self.chat_type}:{self.chat_id}:{self.thread_id or '-'}"

    @cached_property
    def user_memory_key(self) -> str:
        return f"user:{self.channel_tentacle_id}:{self.user_id}"

    def __str__(self) -> str:
        return (
            f"{self.channel_tentacle_id}/{self.chat_type}/{self.chat_id}"
            f"/{self.thread_id or '-'}/{self.user_id}"
        )


class ConversationKey(NamedTuple):
    """The real conversation key: a channel address plus the owning agent. Two
    agents at the same address keep separate conversations, so the conversation a
    `ConversationManager` resolves and caches is identified by both."""

    address: ChannelAddress
    agent_id: str


@sqlalchemy_materia.bless(ConversationModel)
class Conversation(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    external_id: str | None = None

    chat_type: ChatType = Field(frozen=True)
    chat_id: str = Field(frozen=True)
    channel_thread_id: str = Field(default="", frozen=True)
    user_id: str = Field(frozen=True)
    thread_id: uuid.UUID | None = None

    channel_tentacle_id: str = Field(frozen=True)
    agent_tentacle_id: str

    name: str | None = None
    status: str = "active"
    # Approval state for an external coding agent (Claude): the granted mode, and
    # the tools the user allowed for the life of this conversation ("allow for
    # session"), auto-approved without raising a card again.
    permission_mode: ConversationPermissionMode = "default"
    allowed_tools: list[str] = Field(default_factory=list)

    runs: RelationCollection[AgentRun] = Relationships()
    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()

    @cached_property
    def address(self) -> ChannelAddress:
        return ChannelAddress(
            channel_tentacle_id=self.channel_tentacle_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            user_id=self.user_id,
            thread_id=self.channel_thread_id,
        )


class UserProfile(BaseModel):
    model_config = ConfigDict(
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        coerce_numbers_to_str=True,
        frozen=True,
    )

    user_id: str = "0"
    name: str = ""
    nickname: str | None = None
    gender: str | None = None
    age: int | None = None
    title: str | None = None
