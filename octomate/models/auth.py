"""Local account credentials and core-managed runtime authorization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7, SecretStr
from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, SecretString, UTCDateTime
from octomate.types.auth import ApiKeyScope, RuntimeScope


class UserInvitation(Base, TransmuterProxiedMixin):
    """A single-use registration invitation."""

    __tablename__ = "user_invitations"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="SHA-256 digest of an anonymous registration token; never stores the token.",
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Optimistic lock preventing an invitation from being consumed twice.",
    )

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}


class UserSession(Base, TransmuterProxiedMixin):
    """A signed-in session's token hashes and deadlines."""

    __tablename__ = "user_sessions"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[UUID7] = mapped_column(
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


class LinkProfileSession(Base, TransmuterProxiedMixin):
    """One channel profile's pending handoff to an authenticated browser user."""

    __tablename__ = "profile_binding_sessions"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    profile_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="The exact channel profile this session may bind.",
    )
    token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="SHA-256 digest of the one-time URL ticket; never stores the ticket.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Optimistic lock preventing the same profile ticket from binding twice.",
    )

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}


class UserApiKey(Base, TransmuterProxiedMixin):
    """A personal API key's hash, prefix and scopes."""

    __tablename__ = "user_api_keys"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[UUID7] = mapped_column(
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


class RuntimeGrant(Base, TransmuterProxiedMixin):
    """Durable authorization for a user's configured agent in one habitat."""

    __tablename__ = "runtime_grants"
    __table_args__ = (UniqueConstraint("user_id", "agent_tentacle_id", "habitat_name"),)

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[UUID7] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    agent_tentacle_id: Mapped[str] = mapped_column(
        String, nullable=False, comment="Configured agent authorized by this grant."
    )
    habitat_name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Stable Habitat.name for the authorized runtime.",
    )
    scopes: Mapped[list[RuntimeScope]] = mapped_column(
        JSON,
        nullable=False,
        comment="API surfaces this runtime may receive tokens for.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        comment="Revocation retains the grant identity until explicit reauthorization.",
    )


class RuntimeAccessToken(Base, TransmuterProxiedMixin):
    """An expiring bearer hash restricted to one core invocation and resource."""

    __tablename__ = "runtime_access_tokens"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    grant_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("runtime_grants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invocation_id: Mapped[UUID7] = mapped_column(
        Uuid,
        nullable=False,
        index=True,
        comment="Core Invocation.id; allocated before any persisted agent run exists.",
    )
    conversation_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted SHA-256 digest of an opaque access token; never stores the token.",
    )
    resource: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Protected resource identifier this token targets.",
    )
    scopes: Mapped[list[RuntimeScope]] = mapped_column(
        JSON, nullable=False, comment="API surfaces permitted within the owning grant."
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        index=True,
        comment="Authentication deadline independent of expired-row cleanup.",
    )
