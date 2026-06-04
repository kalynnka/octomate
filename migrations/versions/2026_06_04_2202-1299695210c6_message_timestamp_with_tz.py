"""message_timestamp_with_tz

Revision ID: 1299695210c6
Revises: c4f3a1b8e6d2
Create Date: 2026-06-04 22:02:09.696815

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1299695210c6'
down_revision: Union[str, Sequence[str], None] = 'c4f3a1b8e6d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make model_messages.timestamp timezone-aware so the persisted message
    timestamp round-trips as UTC (with the `Z`) instead of as a naive datetime.

    Only Postgres has a distinct `timestamptz` type to switch to; the existing
    naive values were written as UTC, so reinterpret them with `AT TIME ZONE 'UTC'`.
    On sqlite, `timestamp` is text affinity with no tz type — the model's
    `DateTime(timezone=True)` handles tz-aware (de)serialization, so no DDL.
    """
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "model_messages",
            "timestamp",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=True,
            postgresql_using="timestamp AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    """Revert to a naive timestamp column (Postgres only; sqlite is a no-op)."""
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "model_messages",
            "timestamp",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=True,
        )
