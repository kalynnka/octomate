"""Persist storage locations so loaded volumes identify their own storage.

Revision ID: afaf6db24028
Revises: d5553eabfc92
Create Date: 2026-11-13 21:05:14.295535

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "afaf6db24028"
down_revision: str | Sequence[str] | None = "d5553eabfc92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "storage",
                sa.JSON(),
                nullable=False,
                comment="Storage description: kind and storage location, excluding runtime locks, active uses and habitat destinations.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_column("storage")
