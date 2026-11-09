"""Record the folders in each user's root that runs mount.

One table for three kinds: a tree is one thread's working copy of one project (or
of nothing, for a thread in no project), and memory and cache are one per user.
The table starts empty. Today's thread workspaces get no rows here; they keep
working from the disk until the workspace manager moves onto volumes.

Revision ID: d6ddcf644273
Revises: 792e14ef4f19
Create Date: 2026-11-09 11:08:44.597791

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "d6ddcf644273"
down_revision: str | Sequence[str] | None = "792e14ef4f19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "volumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            nullable=False,
            comment="The registered user whose root holds this volume.",
        ),
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            comment="What the volume holds: tree, memory or cache. Never changed.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When the row was written, before its folder was made.",
        ),
        sa.Column(
            "last_used_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When a run last mounted this volume; the idle sweep reads it.",
        ),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            nullable=True,
            comment="The thread this tree is for. Set on trees only.",
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            nullable=True,
            comment="The project whose mirror this tree was forked from; NULL for a thread in no project.",
        ),
        sa.Column(
            "base_ref",
            sa.String(),
            nullable=True,
            comment="The mirror ref this tree was forked from; NULL for the mirror's default branch or a tree in no project.",
        ),
        sa.CheckConstraint(
            "(kind = 'tree') = (thread_id IS NOT NULL)",
            name="ck_volumes_tree_has_thread",
        ),
        sa.CheckConstraint(
            "kind = 'tree' OR (project_id IS NULL AND base_ref IS NULL)",
            name="ck_volumes_tree_only_fields",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_volumes_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_volumes_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name="fk_volumes_thread_id_threads",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_volumes_owner_id"), ["owner_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_volumes_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_volumes_thread_id"), ["thread_id"], unique=False
        )
        batch_op.create_index(
            "uq_volumes_chat_tree",
            ["owner_id", "thread_id"],
            unique=True,
            sqlite_where=sa.text("kind = 'tree' AND project_id IS NULL"),
        )
        batch_op.create_index(
            "uq_volumes_owner_kind",
            ["owner_id", "kind"],
            unique=True,
            sqlite_where=sa.text("kind != 'tree'"),
        )
        batch_op.create_index(
            "uq_volumes_project_tree",
            ["owner_id", "thread_id", "project_id"],
            unique=True,
            sqlite_where=sa.text("kind = 'tree' AND project_id IS NOT NULL"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_volumes_project_tree",
            sqlite_where=sa.text("kind = 'tree' AND project_id IS NOT NULL"),
        )
        batch_op.drop_index(
            "uq_volumes_owner_kind", sqlite_where=sa.text("kind != 'tree'")
        )
        batch_op.drop_index(
            "uq_volumes_chat_tree",
            sqlite_where=sa.text("kind = 'tree' AND project_id IS NULL"),
        )
        batch_op.drop_index(batch_op.f("ix_volumes_thread_id"))
        batch_op.drop_index(batch_op.f("ix_volumes_project_id"))
        batch_op.drop_index(batch_op.f("ix_volumes_owner_id"))

    op.drop_table("volumes")
