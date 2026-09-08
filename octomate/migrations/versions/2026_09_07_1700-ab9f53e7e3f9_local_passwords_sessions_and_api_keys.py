"""Local passwords and revocable credentials belong to the existing user identity.

Existing users remain unenrolled: adding a nullable password hash preserves their
IDs, channel profiles, and legacy credentials. Sessions and API keys store only
digests; refreshing a session uses its version column to reject concurrent reuse.

Revision ID: ab9f53e7e3f9
Revises: 0d0112888c17
Create Date: 2026-09-07 17:00:50.401089

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "ab9f53e7e3f9"
down_revision: str | Sequence[str] | None = "0d0112888c17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "key_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted SHA-256 digest; the API key is disclosed only at issuance.",
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    with op.batch_alter_table("user_api_keys", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_api_keys_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "access_token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted SHA-256 digest of the current opaque access token.",
        ),
        sa.Column(
            "refresh_token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted SHA-256 digest of the current single-use refresh token.",
        ),
        sa.Column(
            "access_expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "refresh_expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="Absolute session deadline; refresh does not extend it.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at", octomate.models.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="Optimistic lock preventing simultaneous refreshes from both succeeding.",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_token_hash"),
        sa.UniqueConstraint("refresh_token_hash"),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_sessions_user_id"), ["user_id"], unique=False
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "password_hash",
                octomate.models.base.SecretString(),
                nullable=True,
                comment="Argon2id password hash; NULL until local sign-in is enrolled.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("password_hash")

    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_sessions_user_id"))

    op.drop_table("user_sessions")
    with op.batch_alter_table("user_api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_api_keys_user_id"))

    op.drop_table("user_api_keys")
