from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from arcanus.base import TransmuterProxiedMixin
from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base
from octomate.models.messages import PydanticJSON
from octomate.types.todos import TodoStatus

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation


class Todo(Base, TransmuterProxiedMixin):
    """One conversation-scoped todo the agent tracks via the todo toolset."""

    __tablename__ = "todos"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ref", name="uq_todos_conversation_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Short hex handle exposed to the model and used by parent_ref/depends_on;
    # unique within a conversation. The uuid PK stays internal.
    ref: Mapped[str] = mapped_column(String, nullable=False, index=True)
    content: Mapped[str] = mapped_column(String, nullable=False)
    active_form: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[TodoStatus] = mapped_column(String, nullable=False, default="pending")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # parent_ref/depends_on hold other todos' `ref`s — app-level, not DB FKs
    # (matching the reference); cycle/blocking is validated in the toolset.
    parent_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    depends_on: Mapped[JsonValue] = mapped_column(
        PydanticJSON, nullable=False, default=list
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

    conversation: Mapped[Conversation] = relationship("Conversation")
