"""Retain complete MCP app invocations for replay without executing tools again.

Revision ID: 4321bb383143
Revises: 741f7a1817c2
Create Date: 2026-10-15 20:38:39.111808

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4321bb383143"
down_revision: str | Sequence[str] | None = "741f7a1817c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("thread_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "app_invocation",
                sa.JSON(),
                nullable=True,
                comment="MCP app invocation: original installation identity, tool declaration, UI resource URI, arguments, and complete result. Retained after uninstall.",
            )
        )


def downgrade() -> None:
    """Remove the column only when no app history would be lost."""
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM thread_messages WHERE kind = 'mcp_app' LIMIT 1")
        )
        .scalar()
        is not None
    ):
        raise RuntimeError("Cannot downgrade while MCP app ledger entries exist")
    # SQLite's native DROP COLUMN preserves the ledger's search triggers.
    with op.batch_alter_table(
        "thread_messages", schema=None, recreate="never"
    ) as batch_op:
        batch_op.drop_column("app_invocation")
