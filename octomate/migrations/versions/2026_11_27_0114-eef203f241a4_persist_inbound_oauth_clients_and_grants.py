"""Persist public-client OAuth consent independently from browser sessions and API keys.

Revision ID: eef203f241a4
Revises: 6376aeb2e4fc
Create Date: 2026-11-27 01:14:50.101725

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "eef203f241a4"
down_revision: str | Sequence[str] | None = "6376aeb2e4fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "redirect_uris",
            sa.JSON(),
            nullable=False,
            comment="Registered callback URIs for this public client.",
        ),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            comment="Permissions this registration may request; not user consent.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "resource",
            sa.String(),
            nullable=False,
            comment="Exact protected resource this consent authorizes.",
        ),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="Idle grant deadline, renewed by successful token refresh; expired grants cannot be renewed.",
        ),
        sa.Column(
            "revoked_at", octomate.models.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["oauth_clients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "client_id", name="uq_oauth_grants_id_client"),
    )
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_grants_client_id"), ["client_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_grants_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_grants_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "oauth_authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column(
            "ticket_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Hash of the browser consent ticket; never the ticket itself.",
        ),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column(
            "code_challenge",
            sa.String(),
            nullable=False,
            comment="S256 PKCE challenge; never the client's verifier.",
        ),
        sa.Column("state", octomate.models.base.SecretString(), nullable=True),
        sa.Column(
            "expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            nullable=True,
            comment="Consent result; absent until the user approves this request.",
        ),
        sa.Column(
            "code_hash",
            octomate.models.base.SecretString(),
            nullable=True,
            comment="Hash of the issued authorization code, retained to recognize code replay.",
        ),
        sa.Column(
            "consumed_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(grant_id IS NULL AND code_hash IS NULL AND consumed_at IS NULL) OR (grant_id IS NOT NULL AND code_hash IS NOT NULL)",
            name="ck_oauth_authorizations_code_grant",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["oauth_clients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id", "client_id"],
            ["oauth_grants.id", "oauth_grants.client_id"],
            name="fk_oauth_authorizations_grant_client",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
        sa.UniqueConstraint("ticket_hash"),
    )
    with op.batch_alter_table("oauth_authorizations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_authorizations_client_id"), ["client_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_authorizations_expires_at"),
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_oauth_authorizations_grant_id"), ["grant_id"], unique=False
        )

    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "access_token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted hash of this generation's opaque access token.",
        ),
        sa.Column(
            "refresh_token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted refresh hash; retained after use to detect replay until grant expiry.",
        ),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            comment="Granted scopes for this generation; refresh may narrow them.",
        ),
        sa.Column(
            "access_expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "refresh_used_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["oauth_grants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_token_hash"),
        sa.UniqueConstraint("refresh_token_hash"),
    )
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_oauth_tokens_grant_id"), ["grant_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_tokens_grant_id"))

    op.drop_table("oauth_tokens")
    with op.batch_alter_table("oauth_authorizations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_authorizations_grant_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_authorizations_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_authorizations_client_id"))

    op.drop_table("oauth_authorizations")
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_grants_user_id"))
        batch_op.drop_index(batch_op.f("ix_oauth_grants_expires_at"))
        batch_op.drop_index(batch_op.f("ix_oauth_grants_client_id"))

    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
