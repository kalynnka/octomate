"""Search thread messages with SQLite FTS5, BM25, and database triggers.

Create the derived full-text index and backfill visible messages from the ledger.
Porter wraps unicode61 for English stemming. Database triggers keep the index in
sync for every ledger write, including Arcanus writes and foreign-key cascades.

Revision ID: 9c5cd2f0c70d
Revises: 2dca6fab4aca
Create Date: 2026-09-13 19:44:18.560142

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from octomate.models.thread import ThreadMessage, ThreadMessageFTS

# revision identifiers, used by Alembic.
revision: str = "9c5cd2f0c70d"
down_revision: str | Sequence[str] | None = "2dca6fab4aca"
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
        """
        CREATE TRIGGER thread_messages_fts_insert
        AFTER INSERT ON thread_messages
        WHEN new.message_text IS NOT NULL AND new.message_text != ''
        BEGIN
            INSERT INTO thread_messages_fts (message_id, message_text)
            VALUES (new.id, new.message_text);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER thread_messages_fts_update
        AFTER UPDATE OF id, message_text ON thread_messages
        WHEN old.id IS NOT new.id OR old.message_text IS NOT new.message_text
        BEGIN
            DELETE FROM thread_messages_fts
            WHERE thread_messages_fts.message_id = old.id;
            INSERT INTO thread_messages_fts (message_id, message_text)
            SELECT new.id, new.message_text
            WHERE new.message_text IS NOT NULL AND new.message_text != '';
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER thread_messages_fts_delete
        AFTER DELETE ON thread_messages
        WHEN old.message_text IS NOT NULL AND old.message_text != ''
        BEGIN
            DELETE FROM thread_messages_fts
            WHERE thread_messages_fts.message_id = old.id;
        END
        """
    )

    op.execute(
        sa.insert(ThreadMessageFTS).from_select(
            [ThreadMessageFTS.message_id, ThreadMessageFTS.message_text],
            sa.select(ThreadMessage.id, ThreadMessage.message_text).where(
                ThreadMessage.message_text.is_not(None),
                ThreadMessage.message_text != "",
            ),
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER thread_messages_fts_delete")
    op.execute("DROP TRIGGER thread_messages_fts_update")
    op.execute("DROP TRIGGER thread_messages_fts_insert")
    op.execute("DROP TABLE thread_messages_fts")
