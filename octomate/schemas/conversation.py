from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Literal, NamedTuple

from arcanus import BaseTransmuter, Relation, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models.conversation import Conversation as ConversationModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.project import Project
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.types.conversations import ConversationPermissionMode

ChatType = Literal["private", "group"]


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
    """Identity of an agent conversation: the owning thread, the agent, and —
    for a context spawned by one of that agent's runs — the subagent. The
    empty subagent_id is the agent's own conversation (the Thread.thread_id
    sentinel convention): a thread owns one bare conversation per agent, so
    every sender in a group thread keys to that one, independent of who woke
    it; each subagent keys to its own."""

    thread_id: uuid.UUID
    agent_id: str
    subagent_id: str = ""


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
    subagent_id: str = Field(
        default="",
        frozen=True,
        description=(
            "Empty for the agent's own conversation in the thread; a subagent's "
            "stable identity (Claude agentId, Codex child thread id, a "
            "commission's name) for a context spawned by one run."
        ),
    )
    parent_conversation_id: uuid.UUID | None = Field(
        default=None,
        frozen=True,
        description=(
            "The conversation whose run spawned this subagent's context — set "
            "iff subagent_id is. Which parent turn drove each child run stays "
            "on the run (parent_run_id)."
        ),
    )

    project_id: uuid.UUID | None = Field(
        default=None,
        frozen=True,
        description=(
            "The declared project this conversation's work is in, taken from its "
            "thread when the conversation is created; None is unattributed, and "
            "runs at the agent's configured directory. Frozen, because its root is "
            "where the runs happen: an external session's history is full of "
            "absolute paths, so re-binding a thread moves new conversations and "
            "leaves this one resumable where it belongs."
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

    project: Relation[Project | None] = Field(
        default_factory=Relation,
        frozen=True,
        exclude=True,
        description="The project this conversation's work is in, eagerly loaded.",
    )
    runs: RelationCollection[AgentRun | ExternalAgentRun] = Relationships()
    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(self.thread_id, self.agent_tentacle_id, self.subagent_id)
