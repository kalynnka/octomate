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
    """Identity of an agent conversation: the owning thread plus the agent. A
    thread owns one conversation per agent, so every sender in a group thread
    keys to the one conversation, independent of who woke it."""

    thread_id: uuid.UUID
    agent_id: str


@sqlalchemy_materia.bless(ConversationModel)
class Conversation(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    external_id: str | None = None

    thread_id: uuid.UUID = Field(
        frozen=True,
        description=(
            "The owning thread; with agent_tentacle_id it is the conversation's "
            "identity."
        ),
    )
    agent_tentacle_id: str = Field(
        frozen=True,
        description=(
            "The owning agent; with thread_id it is the conversation's identity."
        ),
    )

    name: str | None = None
    status: str = "active"
    permission_mode: ConversationPermissionMode = Field(
        default="default",
        description=(
            "Approval mode granted to an external coding agent (Claude): prompt "
            "every gated tool, auto-accept edits, or bypass gating."
        ),
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tools the user allowed for the life of this conversation ('allow for "
            "session'), auto-approved without raising a card again."
        ),
    )

    runs: RelationCollection[AgentRun] = Relationships()
    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(self.thread_id, self.agent_tentacle_id)


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
