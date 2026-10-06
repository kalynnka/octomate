"""Join the two histories that forked from 9c5cd2f0c70d.

Deferred response routing and the stored-file, transcript and handoff-address
revisions were written apart. This changes no schema; it gives alembic one head
again, so a database on either history upgrades to it.

Revision ID: 4f9cac50c4f8
Revises: 39d7ab82a352, a623321f464c
Create Date: 2026-10-05 21:58:10.818031

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "4f9cac50c4f8"
down_revision: str | Sequence[str] | None = ("39d7ab82a352", "a623321f464c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
