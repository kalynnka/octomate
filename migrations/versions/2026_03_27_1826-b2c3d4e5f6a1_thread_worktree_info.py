"""thread worktree_path and branch_name

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-03-27 18:26:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a1"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add worktree_path and branch_name to threads for per-session git isolation."""
    op.add_column("threads", sa.Column("worktree_path", sa.String(), nullable=True))
    op.add_column("threads", sa.Column("branch_name", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove worktree_path and branch_name from threads."""
    op.drop_column("threads", "branch_name")
    op.drop_column("threads", "worktree_path")
