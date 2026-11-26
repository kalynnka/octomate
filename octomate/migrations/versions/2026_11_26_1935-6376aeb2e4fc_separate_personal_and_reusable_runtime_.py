"""Separate personal management from reusable runtime credentials without changing existing bearers.

Revision ID: 6376aeb2e4fc
Revises: 174f49e9cda1
Create Date: 2026-11-26 19:35:04.432193

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "6376aeb2e4fc"
down_revision: str | Sequence[str] | None = "174f49e9cda1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "key_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted SHA-256 digest of the bearer; never stores the bearer itself.",
        ),
        sa.Column(
            "key_prefix",
            sa.String(),
            nullable=False,
            comment="Public hint identifying a key in account settings.",
        ),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            comment="Allowed API surfaces; never grants resource ownership.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at", octomate.models.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "revoked_at", octomate.models.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="Optimistic lock serializing runtime renewal, rotation and revocation across cores.",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_api_keys_id_owner"),
        sa.UniqueConstraint("key_hash"),
    )
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_api_keys_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "runtime_api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            nullable=False,
            comment="Owner constrained to match the base key; enforces one runtime key per user and agent.",
        ),
        sa.Column(
            "runtime_agent_id",
            sa.String(),
            nullable=False,
            comment="Configured agent reusing this user's runtime key.",
        ),
        sa.Column(
            "runtime_nonce",
            sa.Uuid(),
            nullable=False,
            comment="Generation used with the deployment secret to reproduce a runtime bearer.",
        ),
        sa.ForeignKeyConstraint(
            ["id", "user_id"],
            ["api_keys.id", "api_keys.user_id"],
            name="fk_runtime_api_keys_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "runtime_agent_id", name="uq_runtime_api_keys_owner_agent"
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO api_keys "
            "(id, user_id, kind, key_hash, key_prefix, scopes, created_at, expires_at, revoked_at, version) "
            "SELECT id, user_id, 'user', key_hash, key_prefix, scopes, created_at, expires_at, revoked_at, 1 "
            "FROM user_api_keys"
        )
    )
    owner_fk = next(
        foreign_key
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("user_api_keys")
        if foreign_key["constrained_columns"] == ["user_id"]
        and foreign_key["referred_table"] == "users"
    )
    # SQLite leaves this unnamed; batch reflection applies the convention below.
    owner_fk_name = owner_fk["name"] or "fk_user_api_keys_user_id_users"
    with op.batch_alter_table(
        "user_api_keys",
        schema=None,
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
        },
    ) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_api_keys_user_id"))
        batch_op.drop_constraint(owner_fk_name, type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_user_api_keys_api_key", "api_keys", ["id"], ["id"], ondelete="CASCADE"
        )
        batch_op.drop_column("user_id")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("revoked_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("scopes")
        batch_op.drop_column("key_hash")
        batch_op.drop_column("key_prefix")


def downgrade() -> None:
    """The old schema cannot preserve runtime credential identity and renewal state."""
    raise RuntimeError(
        "Restore a pre-upgrade backup to return to the previous API-key schema"
    )
