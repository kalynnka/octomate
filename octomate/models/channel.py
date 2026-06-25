from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from arcanus.base import TransmuterProxiedMixin
from pydantic import JsonValue
from sqlalchemy import (
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
from octomate.models.messages import PydanticJSON

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation
    from octomate.models.messages import ModelMessage

ThreadStatus = Literal["active", "closed"]
ThreadMessageDirection = Literal["inbound", "outbound"]
ChannelActorKind = Literal["human", "agent", "bot", "system"]
MessageBindingKind = Literal[
    "prompt_source",
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
        default=lambda: datetime.now(timezone.utc),
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

    status: Mapped[ThreadStatus] = mapped_column(
        String, nullable=False, default="active", index=True
    )
    prompt_cursor_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("thread_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        index=True,
    )

    messages: Mapped[list[ThreadMessage]] = relationship(
        "ThreadMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        foreign_keys="ThreadMessage.thread_id",
        order_by="ThreadMessage.id",
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
    timestamp: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

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

    sender: Mapped[JsonValue] = mapped_column(PydanticJSON, nullable=False)
    segments: Mapped[JsonValue] = mapped_column(PydanticJSON, nullable=False)
    message_text: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    raw: Mapped[str] = mapped_column(String, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
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
        default=lambda: datetime.now(timezone.utc),
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
