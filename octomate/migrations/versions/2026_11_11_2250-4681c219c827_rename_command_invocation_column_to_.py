"""Name stored command intent consistently with the command API.

Rename the column in place so command payloads and SQLite history-search
triggers survive both upgrade and downgrade.

Revision ID: 4681c219c827
Revises: d6ddcf644273
Create Date: 2026-11-11 22:50:53.176331

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4681c219c827"
down_revision: str | Sequence[str] | None = "d6ddcf644273"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename the existing payload without rebuilding the ledger."""
    op.alter_column(
        "thread_messages",
        "invocation",
        existing_type=sa.JSON(),
        new_column_name="intent",
        existing_comment="Explicit command intent with raw arguments and resolved attachments.",
        existing_nullable=True,
    )


def downgrade() -> None:
    """Restore the old name while retaining command payloads."""
    op.alter_column(
        "thread_messages",
        "intent",
        existing_type=sa.JSON(),
        new_column_name="invocation",
        existing_comment="Explicit command intent with raw arguments and resolved attachments.",
        existing_nullable=True,
    )
