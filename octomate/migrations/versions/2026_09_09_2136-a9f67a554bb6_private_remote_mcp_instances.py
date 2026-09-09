"""Let users configure private MCP instances without deployment code.

Revision ID: a9f67a554bb6
Revises: f973cff9f077
Create Date: 2026-09-10 00:46:32.827535

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "a9f67a554bb6"
down_revision: str | Sequence[str] | None = "f973cff9f077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mcp",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "namespace",
            sa.String(),
            nullable=False,
            comment="Immutable discovery name within the owner's personal MCPs.",
        ),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auth_kind", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "encrypted_token",
            sa.LargeBinary(),
            nullable=True,
            comment="Bearer token encrypted with the owner and instance IDs as authenticated context.",
        ),
        sa.Column(
            "oauth_connection_id",
            sa.Uuid(),
            nullable=True,
            comment="Linked OAuth connection; null until authorization or after the connection is removed.",
        ),
        sa.ForeignKeyConstraint(
            ["oauth_connection_id"], ["oauth_connections.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "namespace", name="uq_mcp_owner_namespace"),
    )
    with op.batch_alter_table("mcp", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mcp_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("mcp", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mcp_user_id"))

    op.drop_table("mcp")
