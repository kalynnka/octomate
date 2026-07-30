"""Authorization-code operations poll nothing.

Revision ID: a7c31d9e5b48
Revises: c38113f96d69
Create Date: 2026-07-31 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c31d9e5b48"
down_revision: Union[str, Sequence[str], None] = "c38113f96d69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("oauth_operations", schema=None) as batch_op:
        batch_op.alter_column(
            "interval_seconds",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    # A polling interval an authorization-code operation never had has to become
    # something before the column can refuse null again.
    op.execute(
        "UPDATE oauth_operations SET interval_seconds = 5 "
        "WHERE interval_seconds IS NULL"
    )
    with op.batch_alter_table("oauth_operations", schema=None) as batch_op:
        batch_op.alter_column(
            "interval_seconds",
            existing_type=sa.Integer(),
            nullable=False,
        )
