"""thread agent_session_id

Revision ID: a1b2c3d4e5f6
Revises: 05d7387410ca
Create Date: 2026-03-27 00:01:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "05d7387410ca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent_session_id to threads for cross-restart session resumption."""
    op.add_column("threads", sa.Column("agent_session_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove agent_session_id from threads."""
    op.drop_column("threads", "agent_session_id")
