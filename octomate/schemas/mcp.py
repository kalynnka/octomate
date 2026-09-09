from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    UrlConstraints,
    model_validator,
)
from uuid_utils.compat import uuid7

from octomate.models import mcp as mcp_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.oauth import HttpsUrl, OAuthConnectionStatus


class NoAuth(BaseModel):
    kind: Literal["none"] = "none"


class BearerAuth(BaseModel):
    kind: Literal["bearer"] = "bearer"
    token: SecretStr = Field(min_length=1, max_length=16384, repr=False)


class OAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["oauth"] = "oauth"
    tentacle_id: str | None = Field(
        default=None,
        min_length=1,
        description="Tentacle supplying app configuration; omit for automatic registration.",
    )


class McpAuthorizationStatus(BaseModel):
    status: OAuthConnectionStatus | Literal[None]


class McpBrowserAuthorizationPending(BaseModel):
    status: Literal["pending_browser"] = "pending_browser"


class McpDeviceAuthorizationPending(BaseModel):
    status: Literal["pending_device"] = "pending_device"
    retry_after_seconds: int = Field(ge=1)


type McpAuthorizationResult = Annotated[
    McpAuthorizationStatus
    | McpBrowserAuthorizationPending
    | McpDeviceAuthorizationPending,
    Field(discriminator="status"),
]


class McpInstallRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    url: Annotated[HttpsUrl, UrlConstraints(max_length=2083)]
    auth: Annotated[NoAuth | BearerAuth | OAuth, Field(discriminator="kind")] = Field(
        default_factory=NoAuth
    )

    @model_validator(mode="after")
    def public_endpoint(self) -> Self:
        if (
            self.url.username
            or self.url.password
            or self.url.query
            or self.url.fragment
        ):
            raise ValueError(
                "MCP endpoints require HTTPS without URL credentials, query, or fragment"
            )
        return self


@sqlalchemy_materia.bless(mcp_models.Mcp)
class Mcp(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: uuid.UUID = Field(frozen=True, exclude=True)
    name: str
    namespace: str = Field(frozen=True)
    url: str = Field(frozen=True)
    enabled: bool = True
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


@sqlalchemy_materia.bless(mcp_models.NoAuthMcp)
class NoAuthMcp(Mcp):
    auth_kind: Literal["none"] = "none"


@sqlalchemy_materia.bless(mcp_models.BearerMcp)
class BearerMcp(Mcp):
    auth_kind: Literal["bearer"] = "bearer"
    encrypted_token: bytes = Field(exclude=True, repr=False)


@sqlalchemy_materia.bless(mcp_models.OAuthMcp)
class OAuthMcp(Mcp):
    auth_kind: Literal["oauth"] = "oauth"
    tentacle_id: str | None = None


type McpVariant = NoAuthMcp | BearerMcp | OAuthMcp
