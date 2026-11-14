"""Step checkpoints and execution ledger, attached to shared agent runs."""

from __future__ import annotations

from datetime import datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from octomate.models.base import Base, UTCDateTime


class StepEvent(Base, TransmuterProxiedMixin):
    """An append-only harness boundary in database insertion order."""

    __tablename__ = "step_events"
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_step_events_key"),
        Index("ix_step_events_run_seq", "run_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String)
    parent_run_id: Mapped[str | None] = mapped_column(String)
    agent_name: Mapped[str | None] = mapped_column(String)
    tool_call_id: Mapped[str | None] = mapped_column(String)
    tool_name: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    meta: Mapped[dict[str, str]] = mapped_column("metadata", JSON, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String, comment="Retry identity scoped to the run; NULL appends every time."
    )


class Snapshot(Base, TransmuterProxiedMixin):
    """A frozen effective context, including any compaction replacements."""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_snapshots_key"),
        CheckConstraint(
            "state IN ('complete', 'interrupted')", name="ck_snapshots_state"
        ),
        Index("ix_snapshots_run_state_seq", "run_id", "state", "seq"),
        Index("ix_snapshots_run_seq", "run_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="Checkpoint insertion order, independent of graph step and source time.",
    )
    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String)
    parent_run_id: Mapped[str | None] = mapped_column(String)
    agent_name: Mapped[str | None] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Complete means tool work is settled, not that the run has finished.",
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String,
        comment="Retry identity scoped to the run; retained with the checkpoint.",
    )


class SnapshotMessage(Base, TransmuterProxiedMixin):
    """An immutable serialized message version, shared across checkpoints.

    These are checkpoint values, not archive owners. An upstream history can
    contain earlier runs, new summaries, or a partially assembled tool response.
    """

    __tablename__ = "snapshot_messages"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="Pydantic AI message JSON with base64 binary content, frozen at checkpoint time.",
    )


class SnapshotMembership(Base, TransmuterProxiedMixin):
    """An ordered checkpoint reference; the same content may occur more than once."""

    __tablename__ = "snapshot_memberships"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_snapshot_memberships_position"),
    )

    snapshot_seq: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("snapshots.seq", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("snapshot_messages.sha256", ondelete="NO ACTION"),
        nullable=False,
        index=True,
    )
    message: Mapped[SnapshotMessage] = relationship(lazy="raise_on_sql")


class ToolEffect(Base, TransmuterProxiedMixin):
    """Latest status of one tool invocation, independently of checkpoint completion."""

    __tablename__ = "tool_effects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="ck_tool_effects_status",
        ),
        Index("ix_tool_effects_run_status", "run_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_call_id: Mapped[str] = mapped_column(String, primary_key=True)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String)
    effect_summary: Mapped[str | None] = mapped_column(String)
