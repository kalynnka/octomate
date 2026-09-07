"""where work happens

`agent_runs.cwd` records the directory a run happened in, as its source reported it:
the hook's own `cwd` for a native session, the transcript's for a turn rebuilt from
disk, and the directory dispatch handed the runtime for an Octomate-driven run. It sits
on the polymorphic base rather than the `external` variant, because an Octomate-driven
run knows its directory too. Empty is what a source reporting no directory stores, and
what every existing row gets — which is exactly what they were.

`projects.origin` then records what put a project in the registry, because a project no
longer has to be declared to exist: a native session running where nothing claims
registers that directory. Rows written before this came from config, so they are
`declared`.

`projects.root` becomes unique, since the root is now the identity — one project per
directory — and the name is a label that steps aside when two roots would be called the
same thing. No existing row can collide: a root was already unique per declaration.

Revision ID: c5917a21ede4
Revises: 9c2b7f4e13da
Create Date: 2026-08-09 14:42:56.258006

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5917a21ede4"
down_revision: str | Sequence[str] | None = "9c2b7f4e13da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "cwd",
                sa.String(),
                server_default="",
                nullable=False,
                comment="The directory this run happened in, as its source reported it; empty when the source reported none. On the base because an Octomate-driven run knows its directory too — where a run ran is not a per-variant question.",
            )
        )

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "origin",
                sa.String(),
                server_default="declared",
                nullable=False,
                comment="What registered this project: `declared` for one registered directly, or the native runtime whose session was found running in it.",
            )
        )
        batch_op.create_unique_constraint("uq_projects_root", ["root"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("uq_projects_root", type_="unique")
        batch_op.drop_column("origin")

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("cwd")
