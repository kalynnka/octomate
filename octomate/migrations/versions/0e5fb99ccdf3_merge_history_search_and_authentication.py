"""Join the independent history-search and authentication migration histories.

Both parents remain valid upgrade starting points. No schema changes are needed;
the FTS index is intentionally absent from ORM metadata and must be preserved.

Revision ID: 0e5fb99ccdf3
Revises: 88a4f648c14d, f973cff9f077
Create Date: 2026-09-10 15:23:26.372201

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0e5fb99ccdf3"
down_revision: str | Sequence[str] | None = ("88a4f648c14d", "f973cff9f077")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two heads without changing their tables or data."""


def downgrade() -> None:
    """Restore both parent heads without changing their tables or data."""
