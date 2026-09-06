"""project dispatch

The one reference that makes a run know which project it is in.

`threads.project_id` records which declared project a thread's work is in, as a real
reference into the registry the `projects` table already holds. SET NULL on delete:
attribution describes a thread, and losing the project must not take its history.

A conversation holds no copy of it: every conversation belongs to a thread, so the
thread's project is the conversation's, and a second column would be the same fact
maintained twice.

Existing rows attribute to nothing, which is exactly what they were: the value is
resolved when the thread is created, and is not revisited.

Revision ID: c4e2a91d0f38
Revises: f1abd4106ca7
Create Date: 2026-08-05 20:23:41.882014

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e2a91d0f38"
down_revision: str | Sequence[str] | None = "f1abd4106ca7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.Uuid(),
                nullable=True,
                comment=(
                    "The declared project this thread's work is in; NULL is "
                    "unattributed, which is what a working directory no project "
                    "claims produces."
                ),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_threads_project_id"), ["project_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_threads_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.drop_constraint("fk_threads_project_id_projects", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_threads_project_id"))
        batch_op.drop_column("project_id")
