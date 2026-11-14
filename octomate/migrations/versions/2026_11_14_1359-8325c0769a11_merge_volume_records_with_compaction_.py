"""Join the volume-record revisions with the compaction-history revisions.

The two chains were written in parallel and touch different tables, so this
revision only gives the history one head again.

Revision ID: 8325c0769a11
Revises: a1a2d6fe9638, 25d11a963652
Create Date: 2026-11-14 13:59:10.726827

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8325c0769a11"
down_revision: str | Sequence[str] | None = ("a1a2d6fe9638", "25d11a963652")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
