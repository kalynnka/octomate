from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import JsonValue
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid, and_
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs
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
    source_address: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    target_address: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    target_mode: Mapped[str] = mapped_column(String, nullable=False, default="main")
    decision: Mapped[JsonValue] = mapped_column(JSON, nullable=True)
    requests: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
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
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    conversation: Mapped[Conversation] = relationship("Conversation")
    questions: Mapped[list[DeferredQuestionAction]] = relationship(
        "DeferredQuestionAction",
        primaryjoin=lambda: and_(
            DeferredActionBatch.id == foreign(DeferredQuestionAction.batch_id),
            DeferredQuestionAction.kind == "question",
        ),
        cascade="all, delete-orphan",
        order_by="DeferredAction.position",
        lazy="selectin",
        overlaps="batch,approvals",
    )
    approvals: Mapped[list[DeferredApprovalAction]] = relationship(
        "DeferredApprovalAction",
        primaryjoin=lambda: and_(
            DeferredActionBatch.id == foreign(DeferredApprovalAction.batch_id),
            DeferredApprovalAction.kind == "approval",
        ),
        cascade="all, delete-orphan",
        order_by="DeferredAction.position",
        lazy="selectin",
        overlaps="batch,questions",
    )


class DeferredAction(Base, TransmuterProxiedMixin):
    """One user-visible pending action inside a deferred batch."""

    __tablename__ = "deferred_actions"
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_abstract": True,
    }

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
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    args: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    meta: Mapped[JsonValue] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    result: Mapped[JsonValue] = mapped_column(JSON, nullable=True)
    platform_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    responder_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    batch: Mapped[DeferredActionBatch] = relationship(
        "DeferredActionBatch",
        overlaps="approvals,questions",
    )


class DeferredQuestionAction(DeferredAction):
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_identity": "question",
    }


class DeferredApprovalAction(DeferredAction):
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_identity": "approval",
    }
