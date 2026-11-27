"""Inbound OAuth registrations, consent, and credentials issued by Octomate."""

from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7, SecretStr
from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, SecretString, UTCDateTime
from octomate.types.auth import ApiKeyScope


class OAuthClient(Base, TransmuterProxiedMixin):
    """A public native application registration, independent of its user grants."""

    __tablename__ = "oauth_clients"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="Registered callback URIs for this public client."
    )
    scopes: Mapped[list[ApiKeyScope]] = mapped_column(
        JSON,
        nullable=False,
        comment="Permissions this registration may request; not user consent.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class OAuthGrant(Base, TransmuterProxiedMixin):
    """One user's consent to one client and resource, independently revocable."""

    __tablename__ = "oauth_grants"
    __table_args__ = (
        UniqueConstraint("id", "client_id", name="uq_oauth_grants_id_client"),
    )

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    client_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID7] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Exact protected resource this consent authorizes.",
    )
    scopes: Mapped[list[ApiKeyScope]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        index=True,
        comment="Idle grant deadline, renewed by successful token refresh; expired grants cannot be renewed.",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}


class OAuthAuthorization(Base, TransmuterProxiedMixin):
    """A browser consent request and its eventual single-use PKCE code."""

    __tablename__ = "oauth_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["grant_id", "client_id"],
            ["oauth_grants.id", "oauth_grants.client_id"],
            ondelete="CASCADE",
            name="fk_oauth_authorizations_grant_client",
        ),
        CheckConstraint(
            "(grant_id IS NULL AND code_hash IS NULL AND consumed_at IS NULL) "
            "OR (grant_id IS NOT NULL AND code_hash IS NOT NULL)",
            name="ck_oauth_authorizations_code_grant",
        ),
    )

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    client_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ticket_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Hash of the browser consent ticket; never the ticket itself.",
    )
    redirect_uri: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[list[ApiKeyScope]] = mapped_column(JSON, nullable=False)
    code_challenge: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="S256 PKCE challenge; never the client's verifier.",
    )
    state: Mapped[SecretStr | None] = mapped_column(SecretString, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
    grant_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
        comment="Consent result; absent until the user approves this request.",
    )
    code_hash: Mapped[SecretStr | None] = mapped_column(
        SecretString,
        nullable=True,
        unique=True,
        comment="Hash of the issued authorization code, retained to recognize code replay.",
    )
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}


class OAuthToken(Base, TransmuterProxiedMixin):
    """One token generation; spent refresh hashes remain until the grant expires."""

    __tablename__ = "oauth_tokens"

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    grant_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("oauth_grants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    access_token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted hash of this generation's opaque access token.",
    )
    refresh_token_hash: Mapped[SecretStr] = mapped_column(
        SecretString,
        nullable=False,
        unique=True,
        comment="Salted refresh hash; retained after use to detect replay until grant expiry.",
    )
    scopes: Mapped[list[ApiKeyScope]] = mapped_column(
        JSON,
        nullable=False,
        comment="Granted scopes for this generation; refresh may narrow them.",
    )
    access_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    refresh_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: ClassVar[MapperArgs] = {"version_id_col": version}
