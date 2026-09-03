from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Self

from arcanus import BaseTransmuter, Relation, RelationCollection, Relationships
from arcanus.base import Identity
from pydantic import AfterValidator, ConfigDict, Field, model_validator
from uuid_utils.compat import uuid7

from octomate.config.agents import AgentRouteModelName
from octomate.models import thread as thread_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.messages import UtcDateTime, native_utc
from octomate.schemas.project import Project
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile
from octomate.types.conversations import ChatType

# The native ids are re-exported under their old home: they moved to
# `types.threads` so `types.permissions` could name them without importing a schema
# that imports it back, and every reader already reaches for them through here.
from octomate.types.threads import (
    CLAUDE_NATIVE_ID as CLAUDE_NATIVE_ID,
)
from octomate.types.threads import (
    CODEX_NATIVE_ID as CODEX_NATIVE_ID,
)
from octomate.types.threads import (
    DEEPSEEK_NATIVE_ID as DEEPSEEK_NATIVE_ID,
)
from octomate.types.threads import (
    NATIVE_TENTACLE_IDS,
    ChannelActorKind,
    MessageBindingKind,
    ThreadKind,
    ThreadMessageDirection,
    ThreadStatus,
)

if TYPE_CHECKING:
    from octomate.schemas.messages import ModelRequest, ModelResponse

# The kinds that are a piece of work, and so can carry a project. A DM or a group chat
# outlives every project in it, and the binding is frozen.
ATTRIBUTABLE_KINDS: frozenset[ThreadKind] = frozenset({"thread", "native_thread"})


@dataclass(frozen=True)
class ThreadKey:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    channel_thread_id: str | None = None

    @classmethod
    def from_address(cls, address: ChannelAddress) -> ThreadKey:
        return cls(
            channel_tentacle_id=address.channel_tentacle_id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
        )

    @property
    def kind(self) -> ThreadKind:
        """What this key names — the chat type, unless a native client owns it."""
        if self.channel_tentacle_id in NATIVE_TENTACLE_IDS:
            return "native_thread"
        return self.chat_type

    def __str__(self) -> str:
        return (
            f"{self.channel_tentacle_id}/{self.chat_type}/"
            f"{self.chat_id}/{self.channel_thread_id or '-'}"
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
    created_at: UtcDateTime = Field(default_factory=lambda: datetime.now(UTC))

    model_messages: RelationCollection[ModelRequest | ModelResponse] = Relationships()

    def __str__(self) -> str:
        """One line of a page: the row id, the `#msg:<id>` handle a brief cites,
        when, who, and what was said."""
        handle = f" #msg:{self.platform_message_id}" if self.platform_message_id else ""
        who = self.agent_tentacle_id or self.user_id or self.actor_kind
        return (
            f"{self.id}{handle} {self.happened_at:%Y-%m-%d %H:%M} "
            f"{self.actor_kind} {who}: {self.message_text or ''}"
        )


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
    source_agent_tentacle_id: str | None = None
    to_agent_tentacle_id: str
    to_model: AgentRouteModelName | None = None
    reason: str = ""
    hint: str = ""
    brief: str = ""
    source_conversation_id: uuid.UUID | None = None
    target_conversation_id: uuid.UUID | None = None
    source_run_id: str | None = None
    source_model_message_id: uuid.UUID | None = None
    created_at: UtcDateTime = Field(default_factory=lambda: datetime.now(UTC))

    def __lt__(self, other: Handoff) -> bool:
        return self.id < other.id

    def __gt__(self, other: Handoff) -> bool:
        return self.id > other.id


@sqlalchemy_materia.bless(thread_models.Thread)
class Thread(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    kind: ThreadKind = Field(
        frozen=True,
        description=(
            "Which surface this thread is. Set from `ThreadKey.kind` when the row is "
            "created; only a `thread` and a `native_thread` may carry a project."
        ),
    )

    chat_type: ChatType
    chat_id: str
    channel_tentacle_id: str
    channel_thread_id: str | None = Field(
        default=None,
        description=("The platform's own thread id; None unless `kind` is `thread`."),
    )
    title: str | None = Field(
        default=None,
        description=(
            "What this thread goes by in a listing. Taken from the first thing a "
            "person said in it, and replaced by a name the runtime grabbed for "
            "itself — a Claude session's own ai-title. None until the thread has "
            "been spoken in, and a listing then falls back to the surface."
        ),
    )
    project_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The declared project this thread's work is in; None is unattributed, "
            "which is what a chat thread and a directory no project claims both "
            "produce. Set once — when the row is created, or later by "
            "`ThreadManager.bind` for a thread that had none — and never moved "
            "after that: an external session's history is full of absolute paths, "
            "so a thread that changed project would resume its sessions somewhere "
            "they do not fit. Not frozen, because settable once from None is a rule "
            "the manager can state and a field cannot."
        ),
    )
    source_cursor_message_id: uuid.UUID | None = None
    status: ThreadStatus = "active"
    created_at: UtcDateTime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: UtcDateTime = Field(default_factory=lambda: datetime.now(UTC))

    project: Relation[Project | None] = Field(
        default_factory=Relation,
        frozen=True,
        exclude=True,
        description="The project this thread's work is in, eagerly loaded.",
    )
    messages: RelationCollection[ThreadMessage] = Relationships()
    handoffs: RelationCollection[Handoff] = Relationships()

    @model_validator(mode="after")
    def kind_agrees_with_the_key(self) -> Self:
        """The channel's key is the fact; this row is our copy of it.

        A copy that contradicts the fact is corrupt, and it would answer questions
        — chiefly whether a project may be attached — with the wrong surface.
        """
        if self.kind != self.key.kind:
            raise ValueError(
                f"thread {self.key} is a {self.key.kind}, "
                f"but the row calls itself a {self.kind}"
            )
        return self

    @property
    def key(self) -> ThreadKey:
        return ThreadKey(
            channel_tentacle_id=self.channel_tentacle_id,
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            channel_thread_id=self.channel_thread_id,
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
