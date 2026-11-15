"""Join the kicks and volumes revisions, which both follow a1a2d6fe9638.

Each was applied to a database before the other existed, so neither can be
moved behind the other: a database already on one would be taken for current
while it lacks the other's tables. Joined here, either kind upgrades through
the revisions it is missing.

Revision ID: 4122899cefcb
Revises: 670d63d8ea49, 78068b2d3971
Create Date: 2026-11-15 04:04:09.929343

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "4122899cefcb"
down_revision: str | Sequence[str] | None = ("670d63d8ea49", "78068b2d3971")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
