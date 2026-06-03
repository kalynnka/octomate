"""require conversation agent_tentacle_id

Revision ID: c4f3a1b8e6d2
Revises: 35a8dfbb3d1a
Create Date: 2026-06-03 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4f3a1b8e6d2"
down_revision: Union[str, Sequence[str], None] = "35a8dfbb3d1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The owning agent is now set at creation; backfill any legacy NULL rows
    # before enforcing the NOT NULL constraint.
    op.execute(
        "UPDATE conversations SET agent_tentacle_id = 'inkling' "
        "WHERE agent_tentacle_id IS NULL"
    )
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "agent_tentacle_id",
            existing_type=sa.String(),
            nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "agent_tentacle_id",
            existing_type=sa.String(),
            nullable=True,
        )
