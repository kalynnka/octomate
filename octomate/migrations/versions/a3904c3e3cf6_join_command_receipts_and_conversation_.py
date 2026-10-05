"""Join the independent command-receipt and conversation-routing migrations.

Revision ID: a3904c3e3cf6
Revises: 8f13851c7e80, 4f9cac50c4f8
Create Date: 2026-10-06 01:33:16.611942

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a3904c3e3cf6"
down_revision: str | Sequence[str] | None = ("8f13851c7e80", "4f9cac50c4f8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
