"""Channel addresses and agent conversations."""

from __future__ import annotations

from dataclasses import field, fields
from functools import cached_property
from typing import Annotated, NamedTuple

from arcanus import BaseTransmuter, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import UUID7, ConfigDict, Field, with_config
from pydantic.dataclasses import dataclass
from typing_extensions import TypedDict
from uuid_utils.compat import uuid7

from octomate.models.conversation import Conversation as ConversationModel
from octomate.models.conversation import ConversationRun as ConversationRunModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.types.conversations import ChatType
from octomate.types.permissions import AgentPermissionMode


@with_config(ConfigDict(extra="allow"))
class AddressMetadata(TypedDict, total=False):
    """What a channel adds to an address to show it; each channel types its own."""

    name: str
    # Lists what is inside; set on a place to open, absent on one to land in.
    inside: str
    # Why a thread cannot land here; absent on an address that can be used.
    barred: str


@dataclass(frozen=True, eq=False)
class ChannelAddress:
    """One surface on a channel — a DM, a group, a thread — and the user spoken to
    on it."""

    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    user_id: str
    # The platform's own thread id — a Slack `thread_ts`, a Lark sub-thread's root
    # message. None on a surface that is not a thread. Never `Thread.id`, which is
    # the row a ledger message points at.
    channel_thread_id: str | None = None
    # Whether anyone besides `user_id` can read this surface, carried from the
    # message that resolved it. `chat_type` cannot answer it: a thread overwrites
    # the type it sits in, so a Slack assistant pane and a group thread both arrive
    # as "thread". False is the safe default — a spell that moves work somewhere
    # private then refuses rather than moving it out of a surface already private.
    shared: bool = False
    # A channel's extras for showing this surface; never part of its identity.
    metadata: AddressMetadata = field(default_factory=AddressMetadata, compare=False)

    # A channel's subclass only types `metadata`, so it is still the same address.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, ChannelAddress) and all(
            getattr(self, field.name) == getattr(other, field.name)
            for field in fields(self)
            if field.compare
        )

    def __hash__(self) -> int:
        return hash(
            tuple(getattr(self, field.name) for field in fields(self) if field.compare)
        )

    @cached_property
    def group_id(self) -> str:
        return self.chat_id if self.chat_type == "group" else ""

    @cached_property
    def topic_memory_key(self) -> str:
        return f"topic:{self.channel_tentacle_id}:{self.chat_type}:{self.chat_id}:{self.channel_thread_id or '-'}"

    @cached_property
    def user_memory_key(self) -> str:
        return f"user:{self.channel_tentacle_id}:{self.user_id}"

    def __str__(self) -> str:
        return (
            f"{self.channel_tentacle_id}/{self.chat_type}/{self.chat_id}"
            f"/{self.channel_thread_id or '-'}/{self.user_id}"
        )


class ConversationKey(NamedTuple):
    """Identity of an agent conversation: the owning thread, the agent, and —
    for a context spawned by one of that agent's runs — the subagent. The
    empty subagent_id is the agent's own conversation (the
    `Thread.channel_thread_id` sentinel convention): a thread owns one bare
    conversation per agent, so
    every sender in a group thread keys to that one, independent of who woke
    it; each subagent keys to its own."""

    thread_id: UUID7
    agent_id: str
    subagent_id: str = ""


@sqlalchemy_materia.bless(ConversationRunModel)
class ConversationRun(BaseTransmuter):
    """A run in a conversation's history: one it ran, or one it was forked with."""

    model_config = ConfigDict(from_attributes=True)

    conversation_id: Annotated[UUID7, Identity]
    run_id: Annotated[str, Identity]


@sqlalchemy_materia.bless(ConversationModel)
class Conversation(BaseTransmuter):
    """One agent's model context within a thread, keyed by thread, agent and
    subagent."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    external_id: str | None = None
    transcript_file_id: UUID7 | None = Field(
        default=None,
        description="Live native transcript, or the independent starting copy of an imported fork.",
    )

    thread_id: UUID7 = Field(
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
    parent_conversation_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description=(
            "The conversation whose run spawned this subagent's context — set "
            "iff subagent_id is. Which parent turn drove each child run stays "
            "on the run (parent_run_id)."
        ),
    )

    name: str | None = None
    status: str = "active"
    permission_mode: AgentPermissionMode | None = Field(
        default=None,
        description=(
            "Approval posture this conversation's agent works under, in that agent's "
            "own vocabulary. None declares nothing, and the agent's configured "
            "default decides. Seeded from the project at creation and owned here "
            "after, so a change made mid-thread is never revoked by a later ensure."
        ),
    )
    effort: str | None = Field(
        default=None,
        description=(
            "Reasoning effort this conversation's runs ask for, one of the levels its "
            "agent's route claims for the model. None declares nothing, and the "
            "runtime's own default decides. A run given an effort of its own, as a "
            "summon is, uses that one instead."
        ),
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tools the user allowed for the life of this conversation ('allow for "
            "session'), auto-approved without raising a card again."
        ),
    )

    runs: RelationCollection[AgentRun | ExternalAgentRun] = Relationships()
    messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()

    @property
    def latest_run(self) -> AgentRun | ExternalAgentRun | None:
        """The last run in the loaded, ordered history, or None before the first run."""
        return self.runs[-1] if self.runs else None

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(self.thread_id, self.agent_tentacle_id, self.subagent_id)
