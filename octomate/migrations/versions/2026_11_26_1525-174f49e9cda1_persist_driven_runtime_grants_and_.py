"""Retain runtime authorization independently of short-lived invocation tokens.

Revision ID: 174f49e9cda1
Revises: 4122899cefcb
Create Date: 2026-11-26 15:25:01.214636

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import octomate.models.base

# revision identifiers, used by Alembic.
revision: str = "174f49e9cda1"
down_revision: str | Sequence[str] | None = "4122899cefcb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "runtime_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "agent_tentacle_id",
            sa.String(),
            nullable=False,
            comment="Configured agent authorized by this grant.",
        ),
        sa.Column(
            "habitat_name",
            sa.String(),
            nullable=False,
            comment="Stable Habitat.name for the authorized runtime.",
        ),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            comment="API surfaces this runtime may receive tokens for.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=True,
            comment="Revocation retains the grant identity until explicit reauthorization.",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "agent_tentacle_id", "habitat_name"),
    )
    op.create_table(
        "runtime_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "invocation_id",
            sa.Uuid(),
            nullable=False,
            comment="Core Invocation.id; allocated before any persisted agent run exists.",
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "token_hash",
            octomate.models.base.SecretString(),
            nullable=False,
            comment="Salted SHA-256 digest of an opaque access token; never stores the token.",
        ),
        sa.Column(
            "resource",
            sa.String(),
            nullable=False,
            comment="Protected resource identifier this token targets.",
        ),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            comment="API surfaces permitted within the owning grant.",
        ),
        sa.Column(
            "created_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            octomate.models.base.UTCDateTime(timezone=True),
            nullable=False,
            comment="Authentication deadline independent of expired-row cleanup.",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["runtime_grants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("runtime_access_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_runtime_access_tokens_conversation_id"),
            ["conversation_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_access_tokens_expires_at"),
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_access_tokens_grant_id"), ["grant_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_access_tokens_invocation_id"),
            ["invocation_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runtime_access_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runtime_access_tokens_invocation_id"))
        batch_op.drop_index(batch_op.f("ix_runtime_access_tokens_grant_id"))
        batch_op.drop_index(batch_op.f("ix_runtime_access_tokens_expires_at"))
        batch_op.drop_index(batch_op.f("ix_runtime_access_tokens_conversation_id"))

    op.drop_table("runtime_access_tokens")
    op.drop_table("runtime_grants")
