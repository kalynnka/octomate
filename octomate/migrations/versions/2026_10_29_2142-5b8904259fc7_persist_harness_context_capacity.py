"""Keep harness-reported context snapshots available after a thread is reopened.

Revision ID: 5b8904259fc7
Revises: ad5c96d8b83a
Create Date: 2026-10-29 21:42:00.430100

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b8904259fc7"
down_revision: str | Sequence[str] | None = "ad5c96d8b83a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_window",
                sa.Integer(),
                nullable=True,
                comment="Context capacity in tokens reported by the harness for this run; NULL when unknown.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "context_used",
                sa.Integer(),
                nullable=True,
                comment="Context tokens used, from the harness occupancy snapshot or the last model request's input usage; NULL when unavailable.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("context_used")
        batch_op.drop_column("context_window")
