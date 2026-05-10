from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from octomate.models.base import Base

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation
    from octomate.models.messages import ModelMessage


class AgentRun(Base, TransmuterProxiedMixin):
    """One agent.run() invocation within a Conversation.

    `id` is the pydantic-ai run_id (uuid7-as-string), which already lives on
    every ModelMessage produced during the run — so messages reference it as
    a foreign key without inventing a synthetic surrogate.
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="runs"
    )
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        back_populates="run",
        cascade="all",
        order_by="ModelMessage.id",
        lazy="selectin",
    )
