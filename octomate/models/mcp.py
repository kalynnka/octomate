from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import Boolean, ForeignKey, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime


class Mcp(Base, TransmuterProxiedMixin):
    __tablename__ = "mcp"
    __table_args__ = (
        UniqueConstraint("user_id", "namespace", name="uq_mcp_owner_namespace"),
    )
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "auth_kind",
        "polymorphic_abstract": True,
        "with_polymorphic": "*",
    }

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Immutable discovery name within the owner's personal MCPs.",
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_kind: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class NoAuthMcp(Mcp):
    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "none"}


class BearerMcp(Mcp):
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_identity": "bearer",
    }

    encrypted_token: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=True,
        comment="Bearer token encrypted with the owner and instance IDs as authenticated context.",
    )


class OAuthMcp(Mcp):
    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "oauth"}

    oauth_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("oauth_connections.id", ondelete="SET NULL"),
        nullable=True,
        comment="Linked OAuth connection; null until authorization or after the connection is removed.",
    )
