from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import SecretStr
from sqlalchemy import JSON, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, SecretString, UTCDateTime
from octomate.types.auth import ApiKeyScope


class UserSession(Base, TransmuterProxiedMixin):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted SHA-256 digest of the current opaque access token.",
    )
    refresh_token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted SHA-256 digest of the current single-use refresh token.",
    )
    access_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    refresh_expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        comment="Absolute session deadline; refresh does not extend it.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Optimistic lock preventing simultaneous refreshes from both succeeding.",
    )

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}


class UserApiKey(Base, TransmuterProxiedMixin):
    __tablename__ = "user_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted SHA-256 digest; the API key is disclosed only at issuance.",
    )
    key_prefix: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Public hint identifying a key in account settings.",
    )
    scopes: Mapped[list[ApiKeyScope]] = mapped_column(
        JSON,
        nullable=False,
        comment="Allowed API surfaces; never grants resource ownership.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
