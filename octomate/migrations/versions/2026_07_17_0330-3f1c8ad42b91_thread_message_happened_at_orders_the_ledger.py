"""thread_message happened_at orders the ledger

The ledger sorts on when a message happened rather than on the uuid7 `id`, which carried
the moment of writing — the same thing until history is replayed, and a session Octomate
learns of mid-conversation has its backfill written after the live turn that revealed it.

`timestamp` becomes `happened_at`: it was never a Unix timestamp, and `created_at` is
right beside it answering a different question — when the row was written, which for
replayed history is a different instant entirely.

Sorting on a nullable column would be worse than the problem: SQLite puts NULLs first,
and every outbound row ever written is one, so every reply would jump to the head of its
thread. So the column becomes NOT NULL, and rows already written take `created_at` — the
only clock they carry, and the one that reproduces exactly the order they already had.

Revision ID: 3f1c8ad42b91
Revises: 7a3e9c1b2f8d
Create Date: 2026-07-17 03:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f1c8ad42b91"
down_revision: str | Sequence[str] | None = "7a3e9c1b2f8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE thread_messages SET timestamp = created_at WHERE timestamp IS NULL"
        )
    )
    # The index is dropped rather than left to the rename: a batch alter rebuilds the
    # table and carries the index over under its old name, which would leave the column
    # indexed twice — once as `ix_thread_messages_timestamp`, over a column no longer
    # called that.
    op.drop_index("ix_thread_messages_timestamp", table_name="thread_messages")
    with op.batch_alter_table("thread_messages") as batch:
        batch.alter_column(
            "timestamp",
            new_column_name="happened_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
    op.create_index(
        "ix_thread_messages_happened_at", "thread_messages", ["happened_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_thread_messages_happened_at", table_name="thread_messages")
    with op.batch_alter_table("thread_messages") as batch:
        batch.alter_column(
            "happened_at",
            new_column_name="timestamp",
            existing_type=sa.DateTime(),
            nullable=True,
        )
    op.create_index("ix_thread_messages_timestamp", "thread_messages", ["timestamp"])
