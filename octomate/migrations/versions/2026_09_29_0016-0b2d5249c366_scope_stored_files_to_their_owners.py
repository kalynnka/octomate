"""Track private file ownership while preserving existing unowned service files.

Revision ID: 0b2d5249c366
Revises: f46e660b1049
Create Date: 2026-09-29 00:16:40.519243

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b2d5249c366"
down_revision: str | Sequence[str] | None = "f46e660b1049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "owner_id",
                sa.Uuid(),
                nullable=True,
                comment="Registered owner; NULL for unowned service files.",
            )
        )
        batch_op.create_index(
            batch_op.f("ix_files_owner_id"), ["owner_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_files_owner_id_users",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_constraint("fk_files_owner_id_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_files_owner_id"))
        batch_op.drop_column("owner_id")
