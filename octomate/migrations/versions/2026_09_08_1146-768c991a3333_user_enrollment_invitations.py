"""Anonymous single-use invitations admit new accounts without reserving usernames.

Revision ID: 768c991a3333
Revises: ab9f53e7e3f9
Create Date: 2026-09-08 12:44:43.784365

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "768c991a3333"
down_revision: str | Sequence[str] | None = "ab9f53e7e3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="SHA-256 digest of an anonymous registration token; never stores the token.",
        ),
        sa.Column(
            "expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="Optimistic lock preventing an invitation from being consumed twice.",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("user_invitations")
