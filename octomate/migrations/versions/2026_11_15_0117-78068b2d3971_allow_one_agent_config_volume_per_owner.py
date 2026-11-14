"""Allow one agent-configuration volume per owner.

The index that keeps an owner to one memory and one cache volume now covers
their configuration volume too. A partial index cannot be altered in place, so
it is dropped and made again under a name that no longer lists what it covers.

Revision ID: 78068b2d3971
Revises: 2746bd1cd91e
Create Date: 2026-11-15 01:17:06.177198

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "78068b2d3971"
down_revision: str | Sequence[str] | None = "2746bd1cd91e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen the one-per-owner index to configuration volumes."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index(
            batch_op.f("uq_volumes_owner_usage"),
            sqlite_where=sa.text("usage IN ('memory', 'cache')"),
        )
        batch_op.create_index(
            "uq_volumes_owner_slot",
            ["owner_id", "usage"],
            unique=True,
            sqlite_where=sa.text("usage IN ('memory', 'cache', 'config')"),
        )


def downgrade() -> None:
    """Restore the index over memory and cache volumes alone."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_volumes_owner_slot",
            sqlite_where=sa.text("usage IN ('memory', 'cache', 'config')"),
        )
        batch_op.create_index(
            batch_op.f("uq_volumes_owner_usage"),
            ["owner_id", "usage"],
            unique=1,
            sqlite_where=sa.text("usage IN ('memory', 'cache')"),
        )
