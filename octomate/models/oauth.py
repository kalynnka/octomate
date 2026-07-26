from __future__ import annotations

import uuid
from datetime import datetime, timezone

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base
from octomate.types.oauth import OAuthConnectionKind, OAuthConnectionStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OAuthConnection(Base, TransmuterProxiedMixin):
    """One user's authorization to one configured provider or MCP resource."""

    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "kind",
            "key",
            name="uq_oauth_connections_user_kind_key",
        ),
        CheckConstraint(
            "status IN ('active', 'invalid')",
            name="ck_oauth_connections_status",
        ),
        CheckConstraint(
            "(kind = 'provider' AND provider IS NOT NULL AND resource_url IS NULL) "
            "OR (kind = 'mcp' AND provider IS NULL AND resource_url IS NOT NULL)",
            name="ck_oauth_connections_variant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[OAuthConnectionKind] = mapped_column(
        String,
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[OAuthConnectionStatus] = mapped_column(
        String,
        nullable=False,
        default="active",
    )
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    account_label: Mapped[str | None] = mapped_column(String, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    encrypted_tokens: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {
        "polymorphic_on": kind,
        "polymorphic_abstract": True,
        "with_polymorphic": "*",
        "version_id_col": version,
    }


class ProviderOAuthConnection(OAuthConnection):
    __mapper_args__ = {"polymorphic_identity": "provider"}

    provider: Mapped[str | None] = mapped_column(String, nullable=True)


class McpOAuthConnection(OAuthConnection):
    __mapper_args__ = {"polymorphic_identity": "mcp"}

    resource_url: Mapped[str | None] = mapped_column(String, nullable=True)
    authorization_server: Mapped[str | None] = mapped_column(String, nullable=True)
    encrypted_client_information: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        nullable=True,
    )


class OAuthTransaction(Base, TransmuterProxiedMixin):
    """Durable, single-use state for one self-service browser authorization."""

    __tablename__ = "oauth_transactions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('provider', 'mcp')",
            name="ck_oauth_transactions_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[OAuthConnectionKind] = mapped_column(
        String,
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    replace_existing: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    ticket_hash: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        unique=True,
        index=True,
    )
    state_hash: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        unique=True,
        index=True,
    )
    encrypted_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    callback_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}
