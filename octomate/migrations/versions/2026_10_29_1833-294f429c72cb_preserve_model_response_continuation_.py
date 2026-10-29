"""Keep response continuation digests and failed model attempts across runs.

Pydantic AI now includes both fields in model responses. Existing messages have
neither value, so nullable columns preserve their history without a backfill.

Revision ID: 294f429c72cb
Revises: ad5c96d8b83a
Create Date: 2026-10-29 18:33:51.709269

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "294f429c72cb"
down_revision: str | Sequence[str] | None = "ad5c96d8b83a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "history_digest",
                sa.String(),
                nullable=True,
                comment="Digest of the response and preceding history used to validate server-side continuation.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "failed_attempts",
                sa.JSON(),
                nullable=True,
                comment="Earlier model request attempts that failed before this response was produced, in order.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.drop_column("failed_attempts")
        batch_op.drop_column("history_digest")
