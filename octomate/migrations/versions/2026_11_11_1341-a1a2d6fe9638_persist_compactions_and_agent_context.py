"""Preserve compaction history and checkpoints without copying conversation messages.

Revision ID: a1a2d6fe9638
Revises: 792e14ef4f19
Create Date: 2026-11-11 15:16:45.131813

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "a1a2d6fe9638"
down_revision: str | Sequence[str] | None = "792e14ef4f19"
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
        "compactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), server_default="octomate", nullable=False),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            nullable=False,
            comment="The conversation whose context was transformed; no model run is required.",
        ),
        sa.Column(
            "trigger",
            sa.String(),
            nullable=False,
            comment="Reported cause: manual, auto, or unknown when the source does not say.",
        ),
        sa.Column(
            "happened_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=True,
            comment="Completion time reported by the source; NULL when unavailable.",
        ),
        sa.Column(
            "recorded_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When Octomate recorded the completed transformation, distinct from its source time.",
        ),
        sa.Column(
            "native_session_id",
            sa.String(),
            nullable=True,
            comment="Native session containing the transformation, when reported.",
        ),
        sa.Column(
            "native_compaction_id",
            sa.String(),
            nullable=True,
            comment="Native transformation identifier, when the source supplies one.",
        ),
        sa.Column(
            "membership_complete",
            sa.Boolean(),
            server_default=sa.text("(false)"),
            nullable=False,
            comment="Whether before and after membership describe the complete effective context; false means unknown or partial.",
        ),
        sa.Column(
            "source",
            sa.String(),
            nullable=True,
            comment="Transcript source containing the external compaction observation.",
        ),
        sa.Column(
            "start_offset",
            sa.Integer(),
            nullable=True,
            comment="Inclusive byte offset of the transcript observation; NULL when unknown.",
        ),
        sa.Column(
            "end_offset",
            sa.Integer(),
            nullable=True,
            comment="Exclusive byte offset of the transcript observation; NULL when unknown.",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("compactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_compactions_conversation_id"),
            ["conversation_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_compactions_kind"), ["kind"], unique=False)

    op.create_table(
        "conversation_compactions",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "compaction_id",
            sa.Uuid(),
            nullable=False,
            comment="Shared compaction identity; deletion is blocked while a conversation still includes it.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=True,
            comment="Zero-based compaction order in this conversation; NULL when the order has not been established.",
        ),
        sa.CheckConstraint(
            "position >= 0", name="ck_conversation_compactions_position"
        ),
        sa.ForeignKeyConstraint(
            ["compaction_id"],
            ["compactions.id"],
            name="fk_conversation_compactions_compaction",
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("conversation_id", "compaction_id"),
        sa.UniqueConstraint(
            "conversation_id", "position", name="uq_conversation_compactions_position"
        ),
    )
    with op.batch_alter_table("conversation_compactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conversation_compactions_compaction_id"),
            ["compaction_id"],
            unique=False,
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
        "compaction_memberships",
        sa.Column("compaction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.String(),
            nullable=False,
            comment="Before is the input context; after is the replacement context, including retained messages.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="Zero-based message position within this stage; independent of message timestamps and IDs.",
        ),
        sa.Column(
            "model_message_id",
            sa.Uuid(),
            nullable=False,
            comment="Referenced archive message; its run or compaction ownership is unchanged and deletion is blocked while referenced.",
        ),
        sa.CheckConstraint(
            "stage IN ('before', 'after')", name="ck_compaction_memberships_stage"
        ),
        sa.CheckConstraint("position >= 0", name="ck_compaction_memberships_position"),
        sa.ForeignKeyConstraint(
            ["compaction_id"], ["compactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_message_id"],
            ["model_messages.id"],
            name="fk_compaction_memberships_message",
            ondelete="NO ACTION",
        ),
        sa.PrimaryKeyConstraint("compaction_id", "stage", "position"),
        sa.UniqueConstraint(
            "compaction_id",
            "stage",
            "model_message_id",
            name="uq_compaction_memberships_message",
        ),
    )
    with op.batch_alter_table("compaction_memberships", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_compaction_memberships_model_message_id"),
            ["model_message_id"],
            unique=False,
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
                "registration_id",
                sa.String(),
                nullable=True,
                comment="Durable registration invocation that claimed this run.",
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

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                server_default="1",
                nullable=False,
                comment="ORM optimistic lock rejecting concurrent conversation updates at flush.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "revision",
                sa.String(),
                nullable=True,
                comment="Opaque token checked and replaced when conversation state is saved; NULL before its first save.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "usage",
                sa.JSON(),
                server_default="{}",
                nullable=False,
                comment="Cumulative RunUsage, including executed tool calls absent from response usage; survives context trimming and replaces the previous total on save.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "deferred_tool_requests",
                sa.JSON(none_as_null=True),
                nullable=True,
                comment="Upstream deferred tool requests waiting for approval or external execution.",
            )
        )

    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "compaction_id",
                sa.Uuid(),
                nullable=True,
                comment="Compaction that created or rewrote this message; NULL for original run messages.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "model_id",
                sa.String(),
                nullable=True,
                comment="Configured model identifier used for the request, distinct from the provider-reported model name.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "canonical_system",
                sa.String(),
                nullable=True,
                comment="Provider system whose keys validate this response's signed or encrypted content, when distinct from provider_name.",
            )
        )
        batch_op.alter_column("run_id", existing_type=sa.VARCHAR(), nullable=True)
        batch_op.create_check_constraint(
            "ck_model_messages_single_owner",
            "(run_id IS NOT NULL AND compaction_id IS NULL) OR (run_id IS NULL AND compaction_id IS NOT NULL)",
        )
        batch_op.create_index(
            batch_op.f("ix_model_messages_compaction_id"),
            ["compaction_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_model_messages_compaction_id_compactions",
            "compactions",
            ["compaction_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    """Reject loss of saved context and compaction content before changing schema."""
    saved_context = op.get_bind().scalar(
        sa.text("SELECT 1 FROM conversations WHERE revision IS NOT NULL LIMIT 1")
    )
    if saved_context is not None:
        raise RuntimeError("Cannot remove saved conversation usage and deferred state.")
    has_context = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM compaction_memberships) "
            "OR EXISTS (SELECT 1 FROM compactions WHERE membership_complete) "
            "OR EXISTS ("
            "SELECT 1 FROM conversation_compactions AS history "
            "JOIN compactions ON compactions.id = history.compaction_id "
            "WHERE history.position IS NOT NULL "
            "OR history.conversation_id != compactions.conversation_id)"
        )
    )
    if has_context:
        raise RuntimeError(
            "Cannot remove compaction membership while context or inherited history is recorded."
        )
    compaction_message = op.get_bind().scalar(
        sa.text("SELECT 1 FROM model_messages WHERE compaction_id IS NOT NULL LIMIT 1")
    )
    if compaction_message is not None:
        raise RuntimeError(
            "Cannot remove compaction ownership while compaction-owned messages exist."
        )
    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.drop_constraint("ck_model_messages_single_owner", type_="check")
        batch_op.drop_constraint(
            "fk_model_messages_compaction_id_compactions", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_model_messages_compaction_id"))
        batch_op.alter_column("run_id", existing_type=sa.VARCHAR(), nullable=False)
        batch_op.drop_column("canonical_system")
        batch_op.drop_column("model_id")
        batch_op.drop_column("compaction_id")

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("version")
        batch_op.drop_column("deferred_tool_requests")
        batch_op.drop_column("usage")
        batch_op.drop_column("revision")

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("metadata")
        batch_op.drop_column("registration_id")
        batch_op.drop_column("agent_name")

    with op.batch_alter_table("snapshot_memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_snapshot_memberships_message_sha256"))

    op.drop_table("snapshot_memberships")
    with op.batch_alter_table("compaction_memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compaction_memberships_model_message_id"))

    op.drop_table("compaction_memberships")
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
    with op.batch_alter_table("conversation_compactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversation_compactions_compaction_id"))

    op.drop_table("conversation_compactions")
    with op.batch_alter_table("compactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compactions_kind"))
        batch_op.drop_index(batch_op.f("ix_compactions_conversation_id"))

    op.drop_table("compactions")
    op.drop_table("snapshot_messages")
