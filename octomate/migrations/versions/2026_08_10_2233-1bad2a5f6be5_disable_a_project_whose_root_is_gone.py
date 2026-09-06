"""disable a project whose root is gone

A project row outlives the directory it names — that is deliberate, since the runs and
threads filed under it still have to read back. But a root that is no longer on disk is
nowhere to work: nothing should resolve to it, and no run should be sent there.

`projects.enabled` is what says so. Startup reconciliation sets it from the filesystem,
in both directions, so a deleted checkout stops resolving and a remounted one starts
again without anyone editing a row. Every existing row is enabled, which is what they
were, and the next reconcile is what corrects any of them.

Revision ID: 1bad2a5f6be5
Revises: c5917a21ede4
Create Date: 2026-08-10 22:33:25.624147

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1bad2a5f6be5"
down_revision: str | Sequence[str] | None = "c5917a21ede4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
                comment="Whether this project is part of the working set. Reconciliation clears it for a root that is no longer on disk, so the row survives for the runs and threads that name it while nothing new resolves to it.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("enabled")
