"""project registry

Create the YAML-declared projects a working directory resolves to. Roots are
absolute local paths; the name defaults to the root's directory. Nothing references a
project yet, and no foreign key ever will — a project reference is a plain
string, as `channel_tentacle_id` and `agent_tentacle_id` already are.

Revision ID: f1abd4106ca7
Revises: e51c06d4b57c
Create Date: 2026-08-04 23:16:38.195183

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1abd4106ca7"
down_revision: str | Sequence[str] | None = "e51c06d4b57c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "name",
            sa.String(),
            nullable=False,
            comment="Stable name for this project; defaults to its root's directory name.",
        ),
        sa.Column(
            "root",
            sa.String(),
            nullable=False,
            comment="The directory this project is, as an absolute local path.",
        ),
        sa.Column(
            "extra_roots",
            sa.JSON(),
            nullable=False,
            comment="Further directories that are also this project, as absolute paths.",
        ),
        sa.Column(
            "description",
            sa.String(),
            nullable=True,
            comment="What this project is; not agent instructions.",
        ),
        sa.Column(
            "permission_mode",
            sa.String(),
            nullable=False,
            comment="Approval mode a conversation in this project starts under.",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("projects")
