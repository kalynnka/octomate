"""Local account and runtime authorization transmuters and issued credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from arcanus import BaseTransmuter, Identity
from pydantic import (
    UUID7,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
)
from uuid_utils.compat import uuid7

from octomate.models import auth as auth_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.user import UserProfile
from octomate.types.auth import ApiKeyScope, RuntimeScope


@sqlalchemy_materia.bless(auth_models.UserInvitation)
class UserInvitation(BaseTransmuter):
    """A single-use registration invitation, stored by token hash."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    token_hash: SecretStr = Field(exclude=True, repr=False)
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(auth_models.UserSession)
class UserSession(BaseTransmuter):
    """A signed-in session: hashed access and refresh tokens with their
    deadlines."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: UUID7
    access_token_hash: SecretStr = Field(exclude=True, repr=False)
    refresh_token_hash: SecretStr = Field(exclude=True, repr=False)
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime = Field(
        description="Absolute session deadline; refresh does not extend it."
    )
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(auth_models.LinkProfileSession)
class LinkProfileSession(BaseTransmuter):
    """One channel profile's pending link ticket, stored by token hash."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    profile_id: UUID7 = Field(
        description="The exact channel profile this session may link."
    )
    token_hash: SecretStr = Field(exclude=True, repr=False)
    expires_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    consumed_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(auth_models.UserApiKey)
class UserApiKey(BaseTransmuter):
    """A personal API key: its hash, public prefix, scopes and lifetime."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: UUID7
    name: str = Field(min_length=1, max_length=100)
    key_hash: SecretStr = Field(exclude=True, repr=False)
    key_prefix: str
    scopes: list[ApiKeyScope] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None


@sqlalchemy_materia.bless(auth_models.RuntimeGrant)
class RuntimeGrant(BaseTransmuter):
    """Durable runtime authorization, retained when revoked to prevent recreation."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: UUID7 = Field(frozen=True)
    agent_tentacle_id: str = Field(
        min_length=1,
        frozen=True,
        description="Configured agent authorized by this grant.",
    )
    habitat_name: str = Field(
        min_length=1,
        frozen=True,
        description="Stable Habitat.name for the authorized runtime.",
    )
    scopes: list[RuntimeScope] = Field(
        min_length=1, description="API surfaces this runtime may receive tokens for."
    )
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: AwareDatetime | None = Field(
        default=None,
        description="Revocation retains the grant identity until explicit reauthorization.",
    )


@sqlalchemy_materia.bless(auth_models.RuntimeAccessToken)
class RuntimeAccessToken(BaseTransmuter):
    """An expiring bearer hash restricted to one core invocation and resource."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    grant_id: UUID7 = Field(frozen=True)
    invocation_id: UUID7 = Field(
        frozen=True,
        description="Core Invocation.id; allocated before any persisted agent run exists.",
    )
    conversation_id: UUID7 = Field(frozen=True)
    token_hash: SecretStr = Field(exclude=True, repr=False)
    resource: str = Field(
        min_length=1,
        frozen=True,
        description="Protected resource identifier this token targets.",
    )
    scopes: list[RuntimeScope] = Field(
        min_length=1, description="API surfaces permitted within the owning grant."
    )
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime = Field(
        description="Authentication deadline independent of expired-row cleanup."
    )


class SessionTokens(BaseModel):
    """Secrets returned only by successful login or refresh."""

    session_id: UUID7
    user_id: UUID7
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime


class LinkProfileInfo(BaseModel):
    """The profile link an authenticated browser is being asked to confirm."""

    profile: UserProfile
    expires_at: AwareDatetime
    # TODO: Model requested scopes and consent once per-profile permissions are enforced.


class LinkProfileAuthorization(LinkProfileInfo):
    """The private host authorization request delivered through a channel or OAuth callback."""

    authorization_uri: AnyHttpUrl = Field(repr=False)


class IssuedApiKey(BaseModel):
    """A freshly issued key with its one-time plaintext token."""

    key: UserApiKey
    token: SecretStr = Field(repr=False)
