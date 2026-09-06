"""Oversized tool returns spill to the database, not the local disk.

Revision ID: e51c06d4b57c
Revises: a7c31d9e5b48
Create Date: 2026-07-31 20:54:02.823705

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e51c06d4b57c"
down_revision: str | Sequence[str] | None = "a7c31d9e5b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tool_output_spills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tool_output_spills", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_tool_output_spills_created_at"),
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_tool_output_spills_handle"), ["handle"], unique=True
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tool_output_spills", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tool_output_spills_handle"))
        batch_op.drop_index(batch_op.f("ix_tool_output_spills_created_at"))

    op.drop_table("tool_output_spills")
