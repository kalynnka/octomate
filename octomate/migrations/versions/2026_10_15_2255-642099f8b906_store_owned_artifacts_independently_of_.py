"""Give artifacts ownership and identity independent of their ledger appearances.

MCP invocations are the first content variant. Existing tool text is not
backfilled: it cannot reconstruct complete invocations. Preserve search triggers
when SQLite rebuilds the ledger to add or remove the artifact foreign key.

Revision ID: 642099f8b906
Revises: 741f7a1817c2
Create Date: 2026-10-15 22:55:07.214204

"""

from collections.abc import Generator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

import octomate.models.base

revision: str = "642099f8b906"
down_revision: str | Sequence[str] | None = "741f7a1817c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@contextmanager
def preserve_ledger_triggers() -> Generator[None]:
    """Retain trigger definitions across SQLite's batch table recreation."""
    triggers = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'thread_messages'"
            )
        )
        .scalars()
        .all()
    )
    yield
    for trigger in triggers:
        op.execute(trigger)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            nullable=False,
            comment="Registered owner, independent of the threads referencing this artifact.",
        ),
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            comment="Content variant determining the artifact viewer.",
        ),
        sa.Column(
            "title",
            sa.String(),
            nullable=False,
            comment="Display title captured when the artifact is created.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="When this artifact was recorded, in UTC.",
        ),
        sa.Column(
            "invocation",
            sa.JSON(),
            nullable=True,
            comment="MCP app invocation: original installation identity, tool declaration, UI resource URI, arguments, and complete result. Retained after uninstall.",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artifacts_owner_id"), ["owner_id"], unique=False
        )

    with (
        preserve_ledger_triggers(),
        op.batch_alter_table("thread_messages", schema=None) as batch_op,
    ):
        batch_op.add_column(
            sa.Column(
                "artifact_id",
                sa.Uuid(),
                nullable=True,
                comment="Referenced artifact; its identity and ownership are independent of this ledger row.",
            )
        )
        batch_op.create_index(
            batch_op.f("ix_thread_messages_artifact_id"), ["artifact_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_thread_artifact",
            "artifacts",
            ["artifact_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Refuse to discard artifacts, even those with no ledger reference."""
    if (
        op.get_bind().execute(sa.text("SELECT 1 FROM artifacts LIMIT 1")).scalar()
        is not None
    ):
        raise RuntimeError("Cannot downgrade while artifacts exist")
    with (
        preserve_ledger_triggers(),
        op.batch_alter_table("thread_messages", schema=None) as batch_op,
    ):
        batch_op.drop_constraint("fk_thread_artifact", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_thread_messages_artifact_id"))
        batch_op.drop_column("artifact_id")

    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_owner_id"))

    op.drop_table("artifacts")
