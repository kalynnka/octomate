"""Keep the latest native transcript available after its client disconnects.

Revision ID: 2422e46779b7
Revises: 0b2d5249c366
Create Date: 2026-09-29 01:50:25.993744

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2422e46779b7"
down_revision: str | Sequence[str] | None = "0b2d5249c366"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "transcript_file_id",
                sa.Uuid(),
                nullable=True,
                comment="Latest uploaded native transcript; independent conversations do not share this reference.",
            )
        )
        batch_op.create_foreign_key(
            "fk_conversations_transcript_file_id_files",
            "files",
            ["transcript_file_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_conversations_transcript_file_id_files", type_="foreignkey"
        )
        batch_op.drop_column("transcript_file_id")
