"""add nullable names to agent runs

Revision ID: 9c72d4a33a10
Revises: 47f580284e75
Create Date: 2026-05-26 15:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9c72d4a33a10"
down_revision: Union[str, Sequence[str], None] = "47f580284e75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("agent_runs", sa.Column("name", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_agent_runs_name"),
        "agent_runs",
        ["name"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_agent_runs_name"), table_name="agent_runs")
    op.drop_column("agent_runs", "name")
