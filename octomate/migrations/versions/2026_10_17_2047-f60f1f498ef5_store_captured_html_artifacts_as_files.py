"""Retain generated visualizations in file storage after their workspace disappears.

Revision ID: f60f1f498ef5
Revises: 642099f8b906
Create Date: 2026-10-17 20:47:09.201107

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f60f1f498ef5"
down_revision: str | Sequence[str] | None = "642099f8b906"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "file_id",
                sa.Uuid(),
                nullable=True,
                comment="Owned HTML file captured from the run workspace for isolated rendering.",
            )
        )
        batch_op.create_foreign_key(
            "fk_artifacts_file_id_files",
            "files",
            ["file_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Refuse to detach saved HTML files from their artifact identities."""
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM artifacts WHERE kind = 'html' LIMIT 1"))
        .scalar()
        is not None
    ):
        raise RuntimeError("Cannot downgrade while HTML artifacts exist")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_constraint("fk_artifacts_file_id_files", type_="foreignkey")
        batch_op.drop_column("file_id")
