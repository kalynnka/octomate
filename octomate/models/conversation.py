from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.messages import ModelMessage
    from octomate.models.runs import AgentRun


class Conversation(Base, TransmuterProxiedMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel_tentacle_id",
            "chat_type",
            "chat_id",
            "thread_id",
            "user_id",
            name="uq_conversations_conversation_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)

    chat_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    channel_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_tentacle_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    runs: Mapped[list[AgentRun]] = relationship(
        "AgentRun",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentRun.started_at",
        lazy="selectin",
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
