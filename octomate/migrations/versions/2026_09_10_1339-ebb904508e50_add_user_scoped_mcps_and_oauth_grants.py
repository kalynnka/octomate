"""Give each user independent MCP configuration and OAuth grants.

Revision ID: ebb904508e50
Revises: f973cff9f077
Create Date: 2026-09-10 13:39:47.247445

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

FOREIGN_KEY_NAMES = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}

revision: str = "ebb904508e50"
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
        sa.Column(
            "instructions",
            sa.String(),
            server_default="",
            nullable=False,
            comment="Tool instructions copied when the MCP is installed.",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auth_kind", sa.String(), nullable=False),
        sa.Column(
            "tentacle_id",
            sa.String(),
            nullable=True,
            comment="Tentacle supplying pre-registered app configuration; null for automatic OAuth client registration.",
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "namespace", name="uq_mcp_owner_namespace"),
    )
    with op.batch_alter_table("mcp", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mcp_user_id"), ["user_id"], unique=False)

    with op.batch_alter_table(
        "oauth_connections", naming_convention=FOREIGN_KEY_NAMES
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mcp_id",
                sa.Uuid(),
                nullable=True,
                comment="MCP owning this grant independently of the connector's app configuration.",
            )
        )
        batch_op.alter_column("subject", existing_type=sa.VARCHAR(), nullable=True)
        batch_op.alter_column(
            "account_label", existing_type=sa.VARCHAR(), nullable=True
        )
        batch_op.drop_constraint(
            batch_op.f("uq_oauth_connections_user_connector"), type_="unique"
        )
        batch_op.create_index(
            "uq_oauth_connections_user_connector",
            ["user_id", "connector_id"],
            unique=True,
            sqlite_where=sa.text("mcp_id IS NULL"),
            postgresql_where=sa.text("mcp_id IS NULL"),
        )
        batch_op.create_unique_constraint("uq_oauth_connections_mcp", ["mcp_id"])
        batch_op.create_foreign_key(
            "fk_oauth_connections_mcp_id_mcp",
            "mcp",
            ["mcp_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table(
        "oauth_operations", naming_convention=FOREIGN_KEY_NAMES
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mcp_id",
                sa.Uuid(),
                nullable=True,
                comment="MCP being authorized; null for a tentacle's own account connection.",
            )
        )
        batch_op.alter_column(
            "profile_id", existing_type=sa.CHAR(length=32), nullable=True
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_operations_mcp_id"), ["mcp_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_oauth_operations_mcp_id_mcp",
            "mcp",
            ["mcp_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    """Refuse to discard authorizations the old schema cannot represent."""
    connection = op.get_bind()
    if (
        connection.execute(
            sa.text(
                "SELECT 1 FROM oauth_connections WHERE mcp_id IS NOT NULL "
                "OR subject IS NULL OR account_label IS NULL UNION ALL "
                "SELECT 1 FROM oauth_operations WHERE mcp_id IS NOT NULL OR profile_id IS NULL"
            )
        ).first()
        is not None
    ):
        raise RuntimeError(
            "The old schema cannot represent existing MCP authorizations"
        )

    with op.batch_alter_table(
        "oauth_operations", naming_convention=FOREIGN_KEY_NAMES
    ) as batch_op:
        batch_op.drop_constraint("fk_oauth_operations_mcp_id_mcp", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_oauth_operations_mcp_id"))
        batch_op.alter_column(
            "profile_id", existing_type=sa.CHAR(length=32), nullable=False
        )
        batch_op.drop_column("mcp_id")

    with op.batch_alter_table(
        "oauth_connections", naming_convention=FOREIGN_KEY_NAMES
    ) as batch_op:
        batch_op.drop_constraint("fk_oauth_connections_mcp_id_mcp", type_="foreignkey")
        batch_op.drop_constraint("uq_oauth_connections_mcp", type_="unique")
        batch_op.drop_index(
            "uq_oauth_connections_user_connector",
            sqlite_where=sa.text("mcp_id IS NULL"),
            postgresql_where=sa.text("mcp_id IS NULL"),
        )
        batch_op.create_unique_constraint(
            batch_op.f("uq_oauth_connections_user_connector"),
            ["user_id", "connector_id"],
        )
        batch_op.alter_column(
            "account_label", existing_type=sa.VARCHAR(), nullable=False
        )
        batch_op.alter_column("subject", existing_type=sa.VARCHAR(), nullable=False)
        batch_op.drop_column("mcp_id")

    with op.batch_alter_table("mcp", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mcp_user_id"))

    op.drop_table("mcp")
