"""Persist workspace references added to Pydantic AI model responses.

Existing responses have no workspace reference and retain NULL.

Revision ID: 741f7a1817c2
Revises: 9413e47fcf52
Create Date: 2026-10-12 01:59:58.259339

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "741f7a1817c2"
down_revision: str | Sequence[str] | None = "9413e47fcf52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "workspace_ref",
                sa.JSON(),
                nullable=True,
                comment="The provider and identifier of the response's workspace, without credentials.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("model_messages", schema=None) as batch_op:
        batch_op.drop_column("workspace_ref")
