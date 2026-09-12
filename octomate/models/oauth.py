from __future__ import annotations

import uuid
from datetime import UTC, datetime

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, UTCDateTime
from octomate.types.oauth import OAuthConnectionStatus


class OAuthOperation(Base, TransmuterProxiedMixin):
    """One owner-bound OAuth authorization that has not yet been consumed."""

    __tablename__ = "oauth_operations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Initiating channel profile; null when authorization starts from an authenticated web session.",
    )
    mcp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("mcp.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="MCP being authorized; null for a tentacle's own account connection.",
    )
    connector_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    encrypted_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
    # Device flow only: the seconds between polls. An authorization-code operation
    # is finished by its callback and has nothing to poll.
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        index=True,
    )


class OAuthConnection(Base, TransmuterProxiedMixin):
    """One registered user's encrypted credentials for an OAuth connector."""

    __tablename__ = "oauth_connections"
    __table_args__ = (
        Index(
            "uq_oauth_connections_user_connector",
            "user_id",
            "connector_id",
            unique=True,
            sqlite_where=text("mcp_id IS NULL"),
            postgresql_where=text("mcp_id IS NULL"),
        ),
        UniqueConstraint("mcp_id", name="uq_oauth_connections_mcp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    mcp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("mcp.id", ondelete="CASCADE"),
        nullable=True,
        comment="MCP owning this grant independently of the connector's app configuration.",
    )
    status: Mapped[OAuthConnectionStatus] = mapped_column(
        String,
        nullable=False,
        default="active",
    )
    encrypted_tokens: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    subject: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Provider account identifier when the authorization flow supplies one.",
    )
    account_label: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="Provider account label when available; generic MCP OAuth need not identify an account.",
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
