"""Keep each installed MCP grant independent of shared app configuration.

Revision ID: 515e2ff21675
Revises: a9f67a554bb6
Create Date: 2026-09-10 03:14:39.811283

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

FOREIGN_KEY_NAMES = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}

revision: str = "515e2ff21675"
down_revision: str | Sequence[str] | None = "a9f67a554bb6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()
    ambiguous = connection.execute(
        sa.text(
            "SELECT 1 FROM mcp WHERE oauth_connection_id IS NOT NULL "
            "GROUP BY oauth_connection_id HAVING COUNT(*) > 1"
        )
    ).first()
    if ambiguous is not None:
        raise RuntimeError(
            "A shared OAuth grant must be separated before this migration"
        )
    with op.batch_alter_table("mcp", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tentacle_id",
                sa.String(),
                nullable=True,
                comment="Tentacle supplying pre-registered app configuration; null for automatic OAuth client registration.",
            )
        )

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

    connection.execute(
        sa.text(
            "UPDATE mcp SET tentacle_id = (SELECT connector_id FROM oauth_connections "
            "WHERE oauth_connections.id = mcp.oauth_connection_id) "
            "WHERE oauth_connection_id IS NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE oauth_connections SET mcp_id = (SELECT id FROM mcp "
            "WHERE mcp.oauth_connection_id = oauth_connections.id) "
            "WHERE id IN (SELECT oauth_connection_id FROM mcp WHERE oauth_connection_id IS NOT NULL)"
        )
    )
    with op.batch_alter_table("mcp", naming_convention=FOREIGN_KEY_NAMES) as batch_op:
        batch_op.drop_constraint(
            "fk_mcp_oauth_connection_id_oauth_connections", type_="foreignkey"
        )
        batch_op.drop_column("oauth_connection_id")

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
        batch_op.add_column(
            sa.Column("oauth_connection_id", sa.CHAR(length=32), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_mcp_oauth_connection_id_oauth_connections",
            "oauth_connections",
            ["oauth_connection_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.drop_column("tentacle_id")
