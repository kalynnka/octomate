"""Record volumes and name stored command intent after the command API.

Volumes start empty: each row is one logical volume, a user's working tree,
memory or cache, or a service-owned Git mirror, with where its bytes live.

The command column is renamed in place rather than dropped and re-added, so
command payloads and SQLite's history-search triggers survive both directions.

Revision ID: 2746bd1cd91e
Revises: a1a2d6fe9638
Create Date: 2026-11-14 14:27:07.800213

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "2746bd1cd91e"
down_revision: str | Sequence[str] | None = "a1a2d6fe9638"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the volume registry and rename the payload without rebuilding the ledger."""
    op.create_table(
        "volumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            nullable=True,
            comment="The registered owner; NULL identifies a service-owned mirror in persisted records.",
        ),
        sa.Column(
            "storage",
            sa.JSON(),
            nullable=False,
            comment="Storage description: kind and storage location, excluding runtime locks, active uses and habitat destinations.",
        ),
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            comment="Content behavior: a directory or a Git-tracked directory, independent of backing storage.",
        ),
        sa.Column(
            "usage",
            sa.String(),
            nullable=True,
            comment="Optional application use: working tree, host mirror, user memory or package cache.",
        ),
        sa.Column(
            "ephemeral",
            sa.Boolean(),
            nullable=False,
            comment="Whether contents are discarded after the last active use; the logical identity remains.",
        ),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            nullable=True,
            comment="The thread using this working volume; absent for other uses.",
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            nullable=True,
            comment="The working tree or mirror's project; absent for chats, the blank mirror and other uses.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When the logical volume was registered.",
        ),
        sa.Column(
            "last_used_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When the application last acquired this volume; used for idle reclamation.",
        ),
        sa.Column(
            "base_ref",
            sa.String(),
            nullable=True,
            comment="The Git ref used for the first checkout; None selects the source's default branch.",
        ),
        sa.Column(
            "mirror_id",
            sa.Uuid(),
            nullable=True,
            comment="The service-owned Git volume supplying this working tree's history and checkpoints.",
        ),
        sa.CheckConstraint(
            "(usage IS NOT NULL AND usage = 'tree' AND thread_id IS NOT NULL) OR (usage IS NOT NULL AND usage = 'mirror' AND kind = 'git' AND owner_id IS NULL AND thread_id IS NULL AND ephemeral = 0) OR ((usage IS NULL OR usage NOT IN ('tree', 'mirror')) AND thread_id IS NULL AND project_id IS NULL)",
            name="ck_volumes_usage_scope",
        ),
        sa.CheckConstraint(
            "kind = 'git' OR (base_ref IS NULL AND mirror_id IS NULL)",
            name="ck_volumes_git_ref",
        ),
        sa.CheckConstraint(
            "owner_id IS NOT NULL OR (usage IS NOT NULL AND usage = 'mirror')",
            name="ck_volumes_persisted_owner",
        ),
        sa.CheckConstraint(
            "usage IS NULL OR usage != 'mirror' OR mirror_id IS NULL",
            name="ck_volumes_mirror_source",
        ),
        sa.ForeignKeyConstraint(
            ["mirror_id"],
            ["volumes.id"],
            name="fk_volumes_mirror_id_volumes",
            ondelete="RESTRICT",
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
            "uq_volumes_blank_mirror",
            ["usage"],
            unique=True,
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NULL"),
        )
        batch_op.create_index(
            "uq_volumes_chat_tree",
            ["owner_id", "thread_id"],
            unique=True,
            sqlite_where=sa.text("usage = 'tree' AND project_id IS NULL"),
        )
        batch_op.create_index(
            "uq_volumes_owner_usage",
            ["owner_id", "usage"],
            unique=True,
            sqlite_where=sa.text("usage IN ('memory', 'cache')"),
        )
        batch_op.create_index(
            "uq_volumes_project_mirror",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NOT NULL"),
        )
        batch_op.create_index(
            "uq_volumes_project_tree",
            ["owner_id", "thread_id", "project_id"],
            unique=True,
            sqlite_where=sa.text("usage = 'tree' AND project_id IS NOT NULL"),
        )

    op.alter_column(
        "thread_messages",
        "invocation",
        existing_type=sa.JSON(),
        new_column_name="intent",
        existing_comment="Explicit command intent with raw arguments and resolved attachments.",
        existing_nullable=True,
    )


def downgrade() -> None:
    """Restore the old column name, keeping command payloads, and drop the registry."""
    op.alter_column(
        "thread_messages",
        "intent",
        existing_type=sa.JSON(),
        new_column_name="invocation",
        existing_comment="Explicit command intent with raw arguments and resolved attachments.",
        existing_nullable=True,
    )
    with op.batch_alter_table("volumes", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_volumes_project_tree",
            sqlite_where=sa.text("usage = 'tree' AND project_id IS NOT NULL"),
        )
        batch_op.drop_index(
            "uq_volumes_project_mirror",
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NOT NULL"),
        )
        batch_op.drop_index(
            "uq_volumes_owner_usage",
            sqlite_where=sa.text("usage IN ('memory', 'cache')"),
        )
        batch_op.drop_index(
            "uq_volumes_chat_tree",
            sqlite_where=sa.text("usage = 'tree' AND project_id IS NULL"),
        )
        batch_op.drop_index(
            "uq_volumes_blank_mirror",
            sqlite_where=sa.text("usage = 'mirror' AND project_id IS NULL"),
        )
        batch_op.drop_index(batch_op.f("ix_volumes_thread_id"))
        batch_op.drop_index(batch_op.f("ix_volumes_project_id"))
        batch_op.drop_index(batch_op.f("ix_volumes_owner_id"))

    op.drop_table("volumes")
