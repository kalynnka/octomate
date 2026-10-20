"""Keep MCP invocations independent of installed or captured UI content.

Existing apps retain their original installation identity and access checks.
Captured resources cannot be represented by the previous schema, so refuse to
downgrade while any exist.

Revision ID: ad5c96d8b83a
Revises: af0f6303cafe
Create Date: 2026-10-19 21:11:02.499530

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ad5c96d8b83a"
down_revision: str | Sequence[str] | None = "af0f6303cafe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

artifacts = sa.table(
    "artifacts",
    sa.column("id", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("invocation", sa.JSON()),
    sa.column("source", sa.JSON()),
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.JSON(),
                nullable=True,
                comment="MCP app content source: original installation identity and namespace, or an owned captured HTML file and resource UI metadata.",
            )
        )

    connection = op.get_bind()
    rows = connection.execute(
        sa.select(artifacts.c.id, artifacts.c.invocation).where(
            artifacts.c.kind == "mcp_app"
        )
    ).all()
    for row in rows:
        invocation = dict(row.invocation)
        source = {
            "kind": "installed",
            "installation_id": invocation.pop("installation_id"),
            "namespace": invocation.pop("namespace"),
        }
        connection.execute(
            artifacts.update()
            .where(artifacts.c.id == row.id)
            .values(source=source, invocation=invocation)
        )


def downgrade() -> None:
    """Downgrade schema."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(artifacts.c.id, artifacts.c.invocation, artifacts.c.source).where(
            artifacts.c.kind == "mcp_app"
        )
    ).all()
    if any(row.source["kind"] != "installed" for row in rows):
        raise RuntimeError("Cannot downgrade while captured MCP apps exist")
    for row in rows:
        invocation = dict(row.invocation)
        invocation.update(
            installation_id=row.source["installation_id"],
            namespace=row.source["namespace"],
        )
        connection.execute(
            artifacts.update()
            .where(artifacts.c.id == row.id)
            .values(invocation=invocation)
        )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_column("source")
