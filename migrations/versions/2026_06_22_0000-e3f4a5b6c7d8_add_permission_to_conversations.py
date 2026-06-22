"""add permission columns to conversations

Revision ID: e3f4a5b6c7d8
Revises: b2c1d0e9f8a7
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'b2c1d0e9f8a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    is_postgres = op.get_bind().dialect.name == "postgresql"
    # `allowed_tools` is a native text[] on Postgres, JSON on SQLite (no array
    # type); the empty-collection literal differs per dialect.
    allowed_tools_type = sa.ARRAY(sa.String()).with_variant(sa.JSON(), "sqlite")
    allowed_tools_default = sa.text("'{}'") if is_postgres else sa.text("'[]'")
    op.add_column(
        'conversations',
        sa.Column(
            'permission_mode',
            sa.String(),
            nullable=False,
            server_default='default',
        ),
    )
    op.add_column(
        'conversations',
        sa.Column(
            'allowed_tools',
            allowed_tools_type,
            nullable=False,
            server_default=allowed_tools_default,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('conversations', 'allowed_tools')
    op.drop_column('conversations', 'permission_mode')
