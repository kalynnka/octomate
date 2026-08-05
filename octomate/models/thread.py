from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from arcanus.base import TransmuterProxiedMixin
from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation
    from octomate.models.messages import ModelMessage
    from octomate.models.project import Project
    from octomate.models.user import UserProfile

ThreadStatus = Literal["active", "closed"]
ThreadMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "request_source",
    "assistant_reply",
    "assistant_send",
]


class MessageBinding(Base, TransmuterProxiedMixin):
    """Association row between the chat ledger and the model ledger."""

    __tablename__ = "message_binding"

    thread_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("thread_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("model_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[MessageBindingKind] = mapped_column(
        String,
        primary_key=True,
    )

    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    thread_message: Mapped[ThreadMessage] = relationship(
        "ThreadMessage",
        overlaps="thread_messages,model_messages",
    )

    model_message: Mapped[ModelMessage] = relationship(
        "ModelMessage",
        overlaps="thread_messages,model_messages",
    )


class Thread(Base, TransmuterProxiedMixin):
    """One IM channel/chat/thread surface, independent of sender and agent."""

    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint(
            "channel_tentacle_id",
            "chat_type",
            "chat_id",
            "thread_id",
            name="uq_threads_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)

    channel_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "The declared project this thread's work is in; NULL is unattributed. "
            "SET NULL rather than CASCADE: attribution describes a thread, and "
            "losing the project it named must not take the thread's history with it."
        ),
    )
    status: Mapped[ThreadStatus] = mapped_column(
        String, nullable=False, default="active", index=True
    )
    source_cursor_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("thread_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    # No back reference: a project would collect every thread ever opened in it, and
    # nothing asks that question. Eager, because every reader of a thread's project
    # wants the row, not the id.
    project: Mapped[Project | None] = relationship("Project", lazy="selectin")

    messages: Mapped[list[ThreadMessage]] = relationship(
        "ThreadMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        foreign_keys="ThreadMessage.thread_id",
        # Conversation order, not insert order. `id` is a uuid7 and so carries the
        # moment of writing, which is the same thing right up until history is replayed:
        # a session Octomate learns of mid-conversation has its backfill written after
        # the live turn that revealed it. It breaks ties, where one instant produced
        # several rows.
        order_by="(ThreadMessage.happened_at, ThreadMessage.id)",
        lazy="selectin",
    )

    handoffs: Mapped[list[Handoff]] = relationship(
        "Handoff",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="Handoff.id",
        lazy="selectin",
    )

    conversations: Mapped[list[Conversation]] = relationship(
        "Conversation",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="Conversation.id",
        lazy="selectin",
    )


class ThreadMessage(Base, TransmuterProxiedMixin):
    """One user-facing message in a thread's chat ledger."""

    __tablename__ = "thread_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)

    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    platform_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    reply_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    happened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    direction: Mapped[ThreadMessageDirection] = mapped_column(
        String, nullable=False, index=True
    )
    actor_kind: Mapped[ChannelActorKind] = mapped_column(
        String, nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, default="", index=True)
    agent_tentacle_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    sender_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_profiles.id"),
        nullable=False,
        index=True,
        comment=(
            "The sender's registry profile row — inbound: the platform account; "
            "outbound: the channel bot or a native session's pseudo-user."
        ),
    )
    # A read-only view keyed off sender_id: the profile's lifecycle belongs to
    # UserManager, so a ledger write never creates or mutates one.
    sender: Mapped[UserProfile] = relationship(
        "UserProfile", lazy="selectin", viewonly=True
    )

    segments: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    message_text: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    raw: Mapped[str] = mapped_column(String, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    thread: Mapped[Thread] = relationship(
        "Thread",
        back_populates="messages",
        foreign_keys=[thread_id],
    )

    model_messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        secondary=MessageBinding.__table__,
        back_populates="thread_messages",
        order_by="ModelMessage.id",
        lazy="selectin",
        viewonly=True,
        overlaps="thread_message,model_message",
    )


class Handoff(Base, TransmuterProxiedMixin):
    """Append-only ownership transfer for a thread."""

    __tablename__ = "channel_handoffs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)

    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    from_agent_tentacle_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_run_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_model_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("model_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    to_agent_tentacle_id: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )
    to_model: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    target_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    hint: Mapped[str] = mapped_column(String, nullable=False, default="")
    brief: Mapped[str] = mapped_column(String, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    thread: Mapped[Thread] = relationship(
        "Thread",
        back_populates="handoffs",
    )

    def __lt__(self, other: Handoff) -> bool:
        return self.id < other.id

    def __gt__(self, other: Handoff) -> bool:
        return self.id > other.id
