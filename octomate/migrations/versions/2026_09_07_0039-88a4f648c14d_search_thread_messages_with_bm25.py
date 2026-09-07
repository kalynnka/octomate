"""Search thread messages with SQLite FTS5 and BM25.

The thread ledger remains the source of truth. This migration adds a derived,
rebuildable full-text index over its visible message text and backfills every row
already in the ledger. Porter wraps SQLite's unicode61 tokenizer because this first
search contract supports English only.

Alembic cannot describe or autogenerate an FTS5 virtual table. The revision was
generated with ``alembic revision --autogenerate`` as usual, then the virtual-table
DDL was added explicitly. A regular FTS table carries the UUID beside its duplicated
text; it avoids pretending this derived index is an Arcanus domain entity.

Revision ID: 88a4f648c14d
Revises: 0d0112888c17
Create Date: 2026-09-07 00:39:45.179715

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "88a4f648c14d"
down_revision: str | Sequence[str] | None = "0d0112888c17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE VIRTUAL TABLE thread_messages_fts USING fts5(
            message_id UNINDEXED,
            message_text,
            tokenize = 'porter unicode61'
        )
        """
    )
    op.execute(
        sa.text(
            """
            INSERT INTO thread_messages_fts(message_id, message_text)
            SELECT id, message_text
            FROM thread_messages
            WHERE message_text IS NOT NULL AND message_text != ''
            """
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE thread_messages_fts")
