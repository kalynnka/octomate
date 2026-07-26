"""OAuth connection persistence.

Store encrypted provider/MCP credentials and durable single-use browser
authorization transactions. No user token or PKCE secret is stored in plaintext.

Revision ID: dc523785b760
Revises: f4a6f02876d3
Create Date: 2026-07-26 15:32:46.560411

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dc523785b760"
down_revision: str | Sequence[str] | None = "f4a6f02876d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("account_label", sa.String(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("encrypted_tokens", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("resource_url", sa.String(), nullable=True),
        sa.Column("authorization_server", sa.String(), nullable=True),
        sa.Column("encrypted_client_information", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'invalid')",
            name="ck_oauth_connections_status",
        ),
        sa.CheckConstraint(
            "(kind = 'provider' AND provider IS NOT NULL AND resource_url IS NULL) "
            "OR (kind = 'mcp' AND provider IS NULL AND resource_url IS NOT NULL)",
            name="ck_oauth_connections_variant",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "kind",
            "key",
            name="uq_oauth_connections_user_kind_key",
        ),
    )
    op.create_table(
        "oauth_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("replace_existing", sa.Boolean(), nullable=False),
        sa.Column("ticket_hash", sa.LargeBinary(), nullable=False),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_data", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('provider', 'mcp')",
            name="ck_oauth_transactions_kind",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oauth_transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_consumed_at"),
            ["consumed_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_expires_at"),
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_profile_id"),
            ["profile_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_state_hash"),
            ["state_hash"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_ticket_hash"),
            ["ticket_hash"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_transactions_user_id"),
            ["user_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("oauth_transactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_user_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_ticket_hash"))
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_state_hash"))
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_profile_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_transactions_consumed_at"))

    op.drop_table("oauth_transactions")
    op.drop_table("oauth_connections")
