from __future__ import annotations

import uuid
from datetime import datetime, timezone

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import (
    JSON,
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
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    encrypted_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )


class OAuthConnection(Base, TransmuterProxiedMixin):
    """One registered user's encrypted credentials for an OAuth connector."""

    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "connector_id",
            name="uq_oauth_connections_user_connector",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[OAuthConnectionStatus] = mapped_column(
        String,
        nullable=False,
        default="active",
    )
    encrypted_tokens: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    account_label: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
