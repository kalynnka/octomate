"""Keep completed transformations identifiable without requiring a model run.

Only identity and source provenance are introduced here. Existing model messages,
run membership, and command receipts are unchanged.

Revision ID: 1f46faa4d027
Revises: 1a83da0aa445
Create Date: 2026-11-04 10:23:02.256008

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

revision: str = "1f46faa4d027"
down_revision: str | Sequence[str] | None = "1a83da0aa445"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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
            "native_id",
            sa.String(),
            nullable=True,
            comment="Native runtime owning the source identifiers; NULL for transformations without one.",
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


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("compactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compactions_kind"))
        batch_op.drop_index(batch_op.f("ix_compactions_conversation_id"))

    op.drop_table("compactions")
