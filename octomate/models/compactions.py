"""Completed context transformations, driven by Octomate or observed externally."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7
from sqlalchemy import ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime
from octomate.types.compactions import CompactionTrigger

if TYPE_CHECKING:
    from octomate.models.messages import ModelMessage


class Compaction(Base, TransmuterProxiedMixin):
    """A completed context transformation, independent of a model run.

    The octomate variant is driven by Octomate, including native compact commands.
    ExternalCompaction records a transformation observed in an external transcript.
    """

    __tablename__ = "compactions"
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "octomate",
        "with_polymorphic": "*",
    }

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(
        String, nullable=False, server_default="octomate", index=True
    )
    conversation_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The conversation whose context was transformed; no model run is required.",
    )
    trigger: Mapped[CompactionTrigger] = mapped_column(
        String,
        nullable=False,
        default="unknown",
        comment="Reported cause: manual, auto, or unknown when the source does not say.",
    )
    happened_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        comment="Completion time reported by the source; NULL when unavailable.",
    )
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When Octomate recorded the completed transformation, distinct from its source time.",
    )
    native_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Native runtime owning the source identifiers; NULL for transformations without one.",
    )
    native_session_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Native session containing the transformation, when reported.",
    )
    native_compaction_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Native transformation identifier, when the source supplies one.",
    )
    messages: Mapped[list[ModelMessage]] = relationship(
        "ModelMessage",
        back_populates="compaction",
        cascade="all",
        order_by="ModelMessage.id",
        lazy="raise_on_sql",
    )


class ExternalCompaction(Compaction):
    """A completed transformation observed in an external runtime's transcript."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "external"}

    source: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Transcript source containing the external compaction observation.",
    )
    start_offset: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Inclusive byte offset of the transcript observation; NULL when unknown.",
    )
    end_offset: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Exclusive byte offset of the transcript observation; NULL when unknown.",
    )
