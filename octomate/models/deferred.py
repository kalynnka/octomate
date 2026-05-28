from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base
from octomate.models.messages import PydanticJSON
from octomate.types.deferred import (
    DeferredActionKind,
    DeferredActionStatus,
    DeferredBatchStatus,
)

if TYPE_CHECKING:
    from octomate.models.conversation import Conversation


class DeferredActionBatch(Base, TransmuterProxiedMixin):
    """One deferred pydantic-ai output waiting for user input/approval."""

    __tablename__ = "deferred_action_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_tentacle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    run_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[DeferredBatchStatus] = mapped_column(
        String,
        nullable=False,
        default="pending",
    )
    source_key: Mapped[dict[str, Any]] = mapped_column(PydanticJSON, nullable=False)
    target_key: Mapped[dict[str, Any]] = mapped_column(PydanticJSON, nullable=False)
    target_mode: Mapped[str] = mapped_column(String, nullable=False, default="main")
    decision: Mapped[dict[str, Any] | None] = mapped_column(PydanticJSON, nullable=True)
    requests: Mapped[dict[str, Any]] = mapped_column(PydanticJSON, nullable=False)
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
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    conversation: Mapped[Conversation] = relationship("Conversation")
    actions: Mapped[list[DeferredAction]] = relationship(
        "DeferredAction",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="DeferredAction.id",
        lazy="selectin",
    )


class DeferredAction(Base, TransmuterProxiedMixin):
    """One user-visible pending action inside a deferred batch."""

    __tablename__ = "deferred_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("deferred_action_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[DeferredActionKind] = mapped_column(
        String,
        nullable=False,
        index=True,
    )
    status: Mapped[DeferredActionStatus] = mapped_column(
        String,
        nullable=False,
        default="pending",
    )
    tool_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    args: Mapped[dict[str, Any]] = mapped_column(PydanticJSON, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        PydanticJSON,
        nullable=False,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(PydanticJSON, nullable=True)
    platform_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    responder_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    batch: Mapped[DeferredActionBatch] = relationship(
        "DeferredActionBatch",
        back_populates="actions",
    )
