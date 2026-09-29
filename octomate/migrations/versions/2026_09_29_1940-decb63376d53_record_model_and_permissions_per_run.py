"""Keep completed-turn settings available for forks and UI labels.

Revision ID: decb63376d53
Revises: 2422e46779b7
Create Date: 2026-09-29 19:40:45.526639

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "decb63376d53"
down_revision: str | Sequence[str] | None = "2422e46779b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_name",
                sa.String(),
                nullable=True,
                comment="Model selected for this run, as reported by its runtime.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "permission_mode",
                sa.String(),
                nullable=True,
                comment="Permission preset used by this run; NULL when unknown or unsupported.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("permission_mode")
        batch_op.drop_column("model_name")
