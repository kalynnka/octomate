"""Store step checkpoints and effects on the service's existing run identity.

Revision ID: 96973e03a30a
Revises: 096de1d1ec5f
Create Date: 2026-11-09 11:11:55.767686

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "96973e03a30a"
down_revision: str | Sequence[str] | None = "096de1d1ec5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "snapshot_messages",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "content",
            sa.LargeBinary(),
            nullable=False,
            comment="Pydantic AI message JSON with base64 binary content, frozen at checkpoint time.",
        ),
        sa.PrimaryKeyConstraint("sha256"),
    )
    op.create_table(
        "snapshots",
        sa.Column(
            "seq",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="Checkpoint insertion order, independent of graph step and source time.",
        ),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("parent_run_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column(
            "timestamp", octomate.models.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "state",
            sa.String(),
            nullable=False,
            comment="Complete means tool work is settled, not that the run has finished.",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(),
            nullable=True,
            comment="Retry identity scoped to the run; retained with the checkpoint.",
        ),
        sa.CheckConstraint(
            "state IN ('complete', 'interrupted')", name="ck_snapshots_state"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_snapshots_key"),
    )
    with op.batch_alter_table("snapshots", schema=None) as batch_op:
        batch_op.create_index("ix_snapshots_run_seq", ["run_id", "seq"], unique=False)
        batch_op.create_index(
            "ix_snapshots_run_state_seq", ["run_id", "state", "seq"], unique=False
        )

    op.create_table(
        "step_events",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column(
            "timestamp", octomate.models.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("parent_run_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(),
            nullable=True,
            comment="Retry identity scoped to the run; NULL appends every time.",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_step_events_key"),
    )
    with op.batch_alter_table("step_events", schema=None) as batch_op:
        batch_op.create_index("ix_step_events_run_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "tool_effects",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "ended_at", octomate.models.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("effect_summary", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="ck_tool_effects_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "tool_call_id"),
    )
    with op.batch_alter_table("tool_effects", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tool_effects_run_status", ["run_id", "status"], unique=False
        )

    op.create_table(
        "snapshot_memberships",
        sa.Column("snapshot_seq", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("message_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_snapshot_memberships_position"),
        sa.ForeignKeyConstraint(
            ["message_sha256"], ["snapshot_messages.sha256"], ondelete="NO ACTION"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_seq"], ["snapshots.seq"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("snapshot_seq", "position"),
    )
    with op.batch_alter_table("snapshot_memberships", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_snapshot_memberships_message_sha256"),
            ["message_sha256"],
            unique=False,
        )

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_name",
                sa.String(),
                nullable=True,
                comment="Harness agent identity, distinct from the run's display name.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "metadata",
                sa.JSON(none_as_null=True),
                nullable=True,
                comment="Harness registration metadata; NULL means unregistered, while an empty object is a valid registration.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "registration_id",
                sa.String(),
                nullable=True,
                comment="Durable registration invocation that claimed this run.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("registration_id")
        batch_op.drop_column("metadata")
        batch_op.drop_column("agent_name")

    with op.batch_alter_table("snapshot_memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_snapshot_memberships_message_sha256"))

    op.drop_table("snapshot_memberships")
    with op.batch_alter_table("tool_effects", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_effects_run_status")

    op.drop_table("tool_effects")
    with op.batch_alter_table("step_events", schema=None) as batch_op:
        batch_op.drop_index("ix_step_events_run_seq")

    op.drop_table("step_events")
    with op.batch_alter_table("snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_snapshots_run_state_seq")
        batch_op.drop_index("ix_snapshots_run_seq")

    op.drop_table("snapshots")
    op.drop_table("snapshot_messages")
