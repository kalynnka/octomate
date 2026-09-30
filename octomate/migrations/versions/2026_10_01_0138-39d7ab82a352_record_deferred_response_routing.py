"""Record how each batch receives its reply instead of inferring suspension.

Live bridges created batches without a decision; the graph suspender persisted
its summon decision. Preserve those response paths when backfilling old batches.

Revision ID: 39d7ab82a352
Revises: cfecde28ca54
Create Date: 2026-10-01 01:38:13.816153

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "39d7ab82a352"
down_revision: str | Sequence[str] | None = "cfecde28ca54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deferred_action_batches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "response_mode",
                sa.String(),
                server_default="live",
                nullable=False,
                comment="Whether a reply answers a live request or resumes a suspended run.",
            )
        )

    batches = sa.table(
        "deferred_action_batches",
        sa.column("response_mode", sa.String()),
        sa.column("decision", sa.JSON()),
    )
    op.execute(
        batches.update()
        .where(batches.c.decision["action"].as_string() == "summon")
        .values(response_mode="resume")
    )


def downgrade() -> None:
    with op.batch_alter_table("deferred_action_batches", schema=None) as batch_op:
        batch_op.drop_column("response_mode")
