"""thread session_name

Revision ID: c3d4e5f6a7b2
Revises: b2c3d4e5f6a1
Create Date: 2026-03-29 13:34:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b2"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add session_name to threads for AI-generated descriptive session names."""
    op.add_column("threads", sa.Column("session_name", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove session_name from threads."""
    op.drop_column("threads", "session_name")
