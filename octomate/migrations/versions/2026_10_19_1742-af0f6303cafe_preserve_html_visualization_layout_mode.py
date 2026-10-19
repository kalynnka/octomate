"""Retain native wide hints so saved visualizations can request more inline space.

Existing artifacts have no retained hint and keep the default layout.

Revision ID: af0f6303cafe
Revises: f60f1f498ef5
Create Date: 2026-10-19 17:42:58.791719

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "af0f6303cafe"
down_revision: str | Sequence[str] | None = "f60f1f498ef5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode",
                sa.String(),
                nullable=True,
                comment="Captured visualization layout hint; wide requests more inline space, null uses the default width.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_column("mode")
