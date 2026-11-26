"""Local account and runtime authorization transmuters and issued credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter, Identity, Transient
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


@sqlalchemy_materia.bless(auth_models.ApiKey)
class ApiKey(BaseTransmuter):
    """Shared credential state, loaded as its concrete personal or runtime variant."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: UUID7 = Field(frozen=True)
    key_hash: SecretStr = Field(exclude=True, repr=False)
    key_prefix: str
    scopes: list[ApiKeyScope] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None
    version: int = Field(default=1, exclude=True, repr=False)
    bearer: Annotated[SecretStr | None, Transient] = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Secret available only on the key returned by issuance; never persisted.",
    )


@sqlalchemy_materia.bless(auth_models.UserApiKey)
class UserApiKey(ApiKey):
    """A personal credential managed through the account API-key endpoints."""

    kind: Literal["user"] = "user"
    name: str = Field(min_length=1, max_length=100)


@sqlalchemy_materia.bless(auth_models.RuntimeApiKey)
class RuntimeApiKey(ApiKey):
    """A core-managed credential reused by one user's configured agent."""

    kind: Literal["runtime"] = "runtime"
    runtime_agent_id: str = Field(
        min_length=1, description="Configured agent reusing this user's runtime key."
    )
    runtime_nonce: UUID7 = Field(
        exclude=True,
        repr=False,
        description="Generation used with the deployment secret to reproduce a runtime bearer.",
    )


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
