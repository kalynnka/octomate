"""Typed persistence for Octomate's inbound, public-client OAuth service."""

from datetime import UTC, datetime
from typing import Annotated

from arcanus import BaseTransmuter, Identity, Transient
from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from uuid_utils.compat import uuid7

from octomate.models import authorization as models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.auth import ApiKeyScope


@sqlalchemy_materia.bless(models.OAuthClient)
class OAuthClient(BaseTransmuter):
    """A public client registration; registration alone authorizes no account."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    name: str = Field(min_length=1, max_length=200)
    redirect_uris: list[str] = Field(min_length=1)
    scopes: list[ApiKeyScope] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


@sqlalchemy_materia.bless(models.OAuthGrant)
class OAuthGrant(BaseTransmuter):
    """One independently revocable account, client, resource, and scope binding."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    client_id: UUID7 = Field(frozen=True)
    user_id: UUID7 = Field(frozen=True)
    resource: str = Field(frozen=True)
    scopes: list[ApiKeyScope] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime = Field(
        description="Idle deadline, renewed by successful token refresh; no absolute lifetime."
    )
    revoked_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(models.OAuthAuthorization)
class OAuthAuthorization(BaseTransmuter):
    """Pending consent, followed by an issued and then consumed PKCE code."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    client_id: UUID7 = Field(frozen=True)
    ticket_hash: SecretStr = Field(exclude=True, repr=False)
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str
    scopes: list[ApiKeyScope] = Field(min_length=1)
    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    state: SecretStr | None = Field(default=None, exclude=True, repr=False)
    expires_at: AwareDatetime
    grant_id: UUID7 | None = None
    code_hash: SecretStr | None = Field(default=None, exclude=True, repr=False)
    consumed_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)

    code: Annotated[SecretStr | None, Transient] = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Code disclosed only on approval; never persisted.",
    )


@sqlalchemy_materia.bless(models.OAuthToken)
class OAuthToken(BaseTransmuter):
    """Hashed token generation, including evidence of a consumed refresh token."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    grant_id: UUID7 = Field(frozen=True)
    access_token_hash: SecretStr = Field(exclude=True, repr=False)
    refresh_token_hash: SecretStr = Field(exclude=True, repr=False)
    scopes: list[ApiKeyScope] = Field(min_length=1)
    access_expires_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    refresh_used_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)

    access_token: Annotated[SecretStr | None, Transient] = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Bearer disclosed only on issuance; never persisted.",
    )
    refresh_token: Annotated[SecretStr | None, Transient] = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Refresh secret disclosed only on issuance; never persisted.",
    )


class OAuthPrincipal(BaseModel):
    """Validated user and client identity, restricted to one token's permissions."""

    grant_id: UUID7
    user_id: UUID7
    client_id: UUID7
    resource: str
    scopes: list[ApiKeyScope]
    expires_at: AwareDatetime
