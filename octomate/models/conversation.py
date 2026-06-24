from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import ARRAY, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.channel import ChannelThread
    from octomate.models.messages import ModelMessage
    from octomate.models.runs import AgentRun


class Conversation(Base, TransmuterProxiedMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "chat_type",
            "chat_id",
            "thread_id",
            "user_id",
            "agent_tentacle_id",
            name="uq_conversations_conversation_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)

    chat_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("channel_threads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    channel_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    permission_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="default"
    )
    # Native string array on Postgres; SQLite (tests/dev) has no array type, so
    # store the list as JSON there. The Python value is `list[str]` either way.
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )

    runs: Mapped[list[AgentRun]] = relationship(
        "AgentRun",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentRun.started_at",
        lazy="selectin",
    )
    channel_thread: Mapped[ChannelThread | None] = relationship(
        "ChannelThread",
        back_populates="conversations",
    )
    # Read-only flat view of every message in the conversation, joined through
    # agent_runs. Writes go through `runs` and each run's `messages`.
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        secondary="agent_runs",
        primaryjoin="Conversation.id == AgentRun.conversation_id",
        secondaryjoin="AgentRun.id == ModelMessage.run_id",
        order_by="(AgentRun.started_at, ModelMessage.id)",
        viewonly=True,
        lazy="selectin",
    )
