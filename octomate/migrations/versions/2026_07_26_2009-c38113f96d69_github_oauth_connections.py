"""Durable encrypted OAuth operations and user connections.

Revision ID: c38113f96d69
Revises: f4a6f02876d3
Create Date: 2026-07-26 20:09:30.826690

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c38113f96d69"
down_revision: str | Sequence[str] | None = "f4a6f02876d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("encrypted_tokens", sa.LargeBinary(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("account_label", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "connector_id", name="uq_oauth_connections_user_connector"
        ),
    )
    with op.batch_alter_table("oauth_connections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_connections_connector_id"),
            ["connector_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_connections_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_connections_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "oauth_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.String(), nullable=False),
        sa.Column("encrypted_data", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["user_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oauth_operations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_connector_id"),
            ["connector_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_consumed_at"), ["consumed_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_profile_id"), ["profile_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("oauth_operations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_operations_user_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_operations_profile_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_operations_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_operations_consumed_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_operations_connector_id"))

    op.drop_table("oauth_operations")
    with op.batch_alter_table("oauth_connections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_connections_user_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_connections_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_connections_connector_id"))

    op.drop_table("oauth_connections")
