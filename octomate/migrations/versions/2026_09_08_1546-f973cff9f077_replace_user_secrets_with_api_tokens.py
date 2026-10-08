"""Remove plaintext user bearers; clients authenticate with hashed API keys.

Existing clients must obtain an API token. Downgrade restores the nullable
column, but cannot recover discarded secrets.

Revision ID: f973cff9f077
Revises: 768c991a3333
Create Date: 2026-09-08 15:46:52.989802

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f973cff9f077"
down_revision: str | Sequence[str] | None = "768c991a3333"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_users_secret"), type_="unique")
        batch_op.drop_column("secret")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("secret", sa.VARCHAR(), nullable=True))
        batch_op.create_unique_constraint(batch_op.f("uq_users_secret"), ["secret"])
