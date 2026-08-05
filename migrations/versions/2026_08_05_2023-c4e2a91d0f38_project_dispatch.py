"""project dispatch

Two references that together make a run know which project it is in.

`threads.project_id` records which declared project a thread's work is in, as a real
reference into the registry the `projects` table already holds. SET NULL on delete:
attribution describes a thread, and losing the project must not take its history.

`conversations.project_id` pins which project a conversation's work is in, and with
it the root its runs happen in. `external_id` already implied a directory, since a
resumable session belongs to the tree it was started in, and this makes the
implication a reference.

Existing rows attribute to nothing, which is exactly what they were: both values are
resolved when the row is created, and neither is revisited.

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

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.Uuid(),
                nullable=True,
                comment=(
                    "The declared project this conversation's work is in, taken "
                    "from its thread when the conversation is created; NULL is "
                    "unattributed, and runs at the agent's configured directory. "
                    "Its root is where the runs happen."
                ),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_conversations_project_id"), ["project_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_conversations_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_conversations_project_id_projects", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_conversations_project_id"))
        batch_op.drop_column("project_id")

    with op.batch_alter_table("threads", schema=None) as batch_op:
        batch_op.drop_constraint("fk_threads_project_id_projects", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_threads_project_id"))
        batch_op.drop_column("project_id")
