"""Record service-owned Git mirrors and each working volume's source.

Revision ID: 25d11a963652
Revises: afaf6db24028
Create Date: 2026-11-13 21:31:44.733349

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "25d11a963652"
down_revision: str | Sequence[str] | None = "afaf6db24028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mirror_id",
                sa.Uuid(),
                nullable=True,
                comment="The service-owned Git volume supplying this working tree's history and checkpoints.",
            )
        )
        batch_op.alter_column(
            "owner_id", existing_type=sa.CHAR(length=32), nullable=True
        )
        batch_op.drop_constraint(batch_op.f("ck_volumes_git_ref"), type_="check")
        batch_op.create_check_constraint(
            "ck_volumes_git_ref",
            "kind = 'git' OR (base_ref IS NULL AND mirror_id IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_volumes_mirror_source",
            "usage IS NULL OR usage != 'mirror' OR mirror_id IS NULL",
        )
        batch_op.create_check_constraint(
            "ck_volumes_persisted_owner",
            "owner_id IS NOT NULL OR (usage IS NOT NULL AND usage = 'mirror')",
        )
        batch_op.drop_constraint(batch_op.f("ck_volumes_usage_scope"), type_="check")
        batch_op.create_check_constraint(
            "ck_volumes_usage_scope",
            "(usage IS NOT NULL AND usage = 'tree' AND thread_id IS NOT NULL) OR (usage IS NOT NULL AND usage = 'mirror' AND kind = 'git' AND owner_id IS NULL AND thread_id IS NULL AND ephemeral = 0) OR ((usage IS NULL OR usage NOT IN ('tree', 'mirror')) AND thread_id IS NULL AND project_id IS NULL)",
        )
        batch_op.create_index(
            "uq_volumes_blank_mirror",
            ["usage"],
            unique=True,
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NULL"),
        )
        batch_op.create_index(
            "uq_volumes_project_mirror",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NOT NULL"),
        )
        batch_op.create_foreign_key(
            "fk_volumes_mirror_id_volumes",
            "volumes",
            ["mirror_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_constraint("fk_volumes_mirror_id_volumes", type_="foreignkey")
        batch_op.drop_index(
            "uq_volumes_project_mirror",
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NOT NULL"),
        )
        batch_op.drop_index(
            "uq_volumes_blank_mirror",
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NULL"),
        )
        batch_op.drop_constraint("ck_volumes_usage_scope", type_="check")
        batch_op.create_check_constraint(
            "ck_volumes_usage_scope",
            "(usage IS NOT NULL AND usage = 'tree' AND thread_id IS NOT NULL) OR ((usage IS NULL OR usage != 'tree') AND thread_id IS NULL AND project_id IS NULL)",
        )
        batch_op.drop_constraint("ck_volumes_persisted_owner", type_="check")
        batch_op.drop_constraint("ck_volumes_mirror_source", type_="check")
        batch_op.drop_constraint("ck_volumes_git_ref", type_="check")
        batch_op.create_check_constraint(
            "ck_volumes_git_ref", "kind = 'git' OR base_ref IS NULL"
        )
        batch_op.alter_column(
            "owner_id", existing_type=sa.CHAR(length=32), nullable=False
        )
        batch_op.drop_column("mirror_id")
