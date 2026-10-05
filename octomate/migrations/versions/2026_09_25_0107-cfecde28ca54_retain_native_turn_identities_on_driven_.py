"""Retain native turn identities so transcript replay can recognize driven work.

Preserve existing session IDs under their native name in both directions.
Existing runs have no reliable cross-runtime turn key to backfill. Their history
is preserved; new driven runs record the identity needed for deduplication.

Revision ID: cfecde28ca54
Revises: 9c5cd2f0c70d
Create Date: 2026-09-25 01:07:30.234315

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cfecde28ca54"
down_revision: str | Sequence[str] | None = "9c5cd2f0c70d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "native_id",
                sa.String(),
                nullable=True,
                comment="The native runtime owning this run's native session and turn IDs.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "native_turn_id",
                sa.String(),
                nullable=True,
                comment="The native ingest turn key, retained on driven runs to recognize replay.",
            )
        )
        batch_op.add_column(sa.Column("native_session_id", sa.String(), nullable=True))
        batch_op.drop_index(batch_op.f("ix_agent_runs_external_session_id"))
        batch_op.create_index(
            batch_op.f("ix_agent_runs_native_id"), ["native_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_agent_runs_native_session_id"),
            ["native_session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_agent_runs_native_turn_id"), ["native_turn_id"], unique=False
        )

    op.execute("UPDATE agent_runs SET native_session_id = external_session_id")
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("external_session_id")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("external_session_id", sa.VARCHAR(), nullable=True)
        )
        batch_op.drop_index(batch_op.f("ix_agent_runs_native_turn_id"))
        batch_op.drop_index(batch_op.f("ix_agent_runs_native_session_id"))
        batch_op.drop_index(batch_op.f("ix_agent_runs_native_id"))
        batch_op.create_index(
            batch_op.f("ix_agent_runs_external_session_id"),
            ["external_session_id"],
            unique=False,
        )

    op.execute("UPDATE agent_runs SET external_session_id = native_session_id")
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("native_session_id")
        batch_op.drop_column("native_turn_id")
        batch_op.drop_column("native_id")
