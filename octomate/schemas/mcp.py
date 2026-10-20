"""MCP installations: their auth variants, install requests, authorization results
and summaries."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cached_property
from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from fastmcp.apps import AppConfig, ResourceCSP
from mcp.types import CallToolResult, Tool
from pydantic import (
    UUID7,
    AliasChoices,
    AliasPath,
    AnyUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    UrlConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from uuid_utils.compat import uuid7

from octomate.models import mcp as mcp_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.oauth import HttpsUrl, OAuthConnectionStatus, OAuthFlowKind


class McpAppToolMeta(BaseModel):
    """Extract the app URI, including the SDK's legacy spelling.

    Unrelated metadata is ignored here and preserved on the original Tool.
    """

    resource_uri: Annotated[AnyUrl, UrlConstraints(allowed_schemes=["ui"])] | None = (
        Field(
            default=None,
            validation_alias=AliasChoices(
                AliasPath("ui", "resourceUri"), "ui/resourceUri"
            ),
            description="The UI resource advertised by a tool, when present.",
        )
    )


class McpAppInvocation(BaseModel):
    """A completed MCP app call, retained independently of its content source.

    The tool declaration and complete result are snapshots: replay needs neither
    another tool call nor a namespace lookup that could resolve a replacement
    installation. The artifact's source determines how its UI resource is loaded.
    """

    provider_name: str = Field(
        description="The installation's display name or runtime server name at execution."
    )
    tool: Tool | None = Field(
        default=None,
        description="The upstream tool declaration when supplied by the runtime.",
    )
    resource_uri: Annotated[AnyUrl, UrlConstraints(allowed_schemes=["ui"])] = Field(
        description="The UI resource declared by the tool.",
    )
    arguments: dict[str, JsonValue] = Field(
        description="Original arguments passed to the upstream tool, without clipping."
    )
    result: CallToolResult = Field(
        description="Complete upstream result, including structured content and metadata."
    )


class McpAppResourceCSP(ResourceCSP):
    """Declared origins, restricted to HTTP(S) and network-only WS(S) sources.

    Subdomain wildcards are supported. CSP keywords, credentials, paths, queries,
    fragments, and directive separators are refused before header construction.
    """

    @field_validator(
        "connect_domains", "resource_domains", "frame_domains", "base_uri_domains"
    )
    @classmethod
    def validate_domains(
        cls, domains: list[str] | None, info: ValidationInfo
    ) -> list[str] | None:
        if domains is None:
            return None
        schemes = {"http", "https"}
        if info.field_name == "connect_domains":
            schemes.update({"ws", "wss"})
        validated = []
        for domain in domains:
            if any(char.isspace() or char in ";,'\"\\" for char in domain):
                raise ValueError("CSP sources must be HTTP(S) or WS(S) origins")
            url = AnyUrl(domain)
            if (
                url.scheme not in schemes
                or url.host is None
                or "*" in url.host.removeprefix("*.")
                or url.username is not None
                or url.password is not None
                or url.path not in {None, "/"}
                or url.query is not None
                or url.fragment is not None
            ):
                raise ValueError("CSP sources must be HTTP(S) or WS(S) origins")
            validated.append(str(url).removesuffix("/"))
        return validated


class McpAppResourceUI(AppConfig):
    """Resource UI metadata; requested permissions and domains are not grants."""

    csp: McpAppResourceCSP | None = Field(
        default=None, description="Validated declared origins for sandbox policy."
    )


class McpAppResource(BaseModel):
    """A single matching MCP Apps HTML resource, decoded for the sandbox.

    HTML is untrusted and must only be rendered by the isolated sandbox. Resource
    metadata describes requests; the host still applies its own sandbox policy.
    """

    uri: Annotated[AnyUrl, UrlConstraints(allowed_schemes=["ui"])] = Field(
        description="The saved invocation's UI resource URI."
    )
    html: str = Field(
        min_length=1, description="Resource text or strictly decoded UTF-8 HTML."
    )
    ui: McpAppResourceUI = Field(
        default_factory=McpAppResourceUI, description="The resource's UI metadata."
    )

    @field_validator("html")
    @classmethod
    def validate_html(cls, value: str) -> str:
        value.encode("utf-8")
        if not value.strip():
            raise ValueError("MCP app HTML must not be empty")
        return value


class NoAuth(BaseModel):
    """No authentication."""

    kind: Literal["none"] = "none"


class BearerAuth(BaseModel):
    """A static bearer token."""

    kind: Literal["bearer"] = "bearer"
    token: SecretStr = Field(min_length=1, max_length=16384, repr=False)


class OAuth(BaseModel):
    """OAuth, authorized by the installing user."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["oauth"] = "oauth"


class McpAuthorizationStatus(BaseModel):
    """The stored grant's status, or null when there is none."""

    status: OAuthConnectionStatus | Literal[None]


class McpBrowserAuthorizationPending(BaseModel):
    """A browser authorization is waiting on the user."""

    status: Literal["pending_browser"] = "pending_browser"


class McpDeviceAuthorizationPending(BaseModel):
    """A device authorization is waiting on the user; poll again after the
    interval."""

    status: Literal["pending_device"] = "pending_device"
    retry_after_seconds: int = Field(ge=1)


type McpAuthorizationResult = Annotated[
    McpAuthorizationStatus
    | McpBrowserAuthorizationPending
    | McpDeviceAuthorizationPending,
    Field(discriminator="status"),
]


class McpInstallRequest(BaseModel):
    """What installing an MCP takes: a name, a namespace, an HTTPS endpoint, and
    either an auth choice or the tentacle that supplies it."""

    name: str = Field(min_length=1, max_length=100)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    url: Annotated[HttpsUrl, UrlConstraints(max_length=2083)]
    auth: Annotated[NoAuth | BearerAuth | OAuth, Field(discriminator="kind")] | None = (
        None
    )
    tentacle_id: str | None = Field(
        default=None,
        min_length=1,
        description="Configured tentacle supplying the MCP and its authentication.",
    )

    @model_validator(mode="after")
    def public_endpoint(self) -> Self:
        if self.tentacle_id is not None and self.auth is not None:
            raise ValueError("A tentacle supplies its own authentication")
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
    """A user's MCP installation, polymorphic on how it authenticates."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    user_id: UUID7 = Field(frozen=True, exclude=True)
    name: str
    namespace: str = Field(frozen=True)
    url: str = Field(frozen=True)
    instructions: str = ""
    tentacle_id: str | None = None
    enabled: bool = True
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def summary(self) -> McpServerSummary:
        match self:
            case NoAuthMcp() | BearerMcp() | OAuthMcp():
                auth_kind = self.auth_kind
            case _:
                raise TypeError("MCP info requires a concrete authentication type")
        return McpServerSummary(
            id=self.id,
            namespace=self.namespace,
            name=self.name,
            enabled=self.enabled,
            auth_kind=auth_kind,
        )


@sqlalchemy_materia.bless(mcp_models.NoAuthMcp)
class NoAuthMcp(Mcp):
    """An installation that sends no credentials."""

    auth_kind: Literal["none"] = "none"


@sqlalchemy_materia.bless(mcp_models.BearerMcp)
class BearerMcp(Mcp):
    """An installation authenticating with an encrypted bearer token."""

    auth_kind: Literal["bearer"] = "bearer"
    encrypted_token: bytes = Field(min_length=1, exclude=True, repr=False)


@sqlalchemy_materia.bless(mcp_models.OAuthMcp)
class OAuthMcp(Mcp):
    """An installation authenticating with the user's OAuth grant."""

    auth_kind: Literal["oauth"] = "oauth"


class McpTentacleInfo(BaseModel):
    """A configured MCP tentacle as offered for installation."""

    id: str
    name: str
    url: str
    auth_kind: Literal["none", "bearer", "oauth"]


class McpOAuthSummary(BaseModel):
    """An OAuth installation's grant status and the flows it supports."""

    status: (
        OAuthConnectionStatus | Literal["pending_browser", "pending_device", None]
    ) = Field(
        description="Stored grant or live authorization status; null means neither exists."
    )
    flows: list[OAuthFlowKind]


class McpServerSummary(BaseModel):
    """An installation as listed: identity, state and auth kind."""

    id: UUID7
    namespace: str
    name: str
    enabled: bool
    auth_kind: Literal["none", "bearer", "oauth"]
    oauth: McpOAuthSummary | None = None


class McpToolCatalog(BaseModel):
    """The tools and instructions an installation currently serves."""

    instructions: str = Field(description="The selected provider's tool instructions.")
    tools: list[Tool] = Field(description="The selected provider's MCP tool schemas.")

    @cached_property
    def tools_by_name(self) -> dict[str, Tool]:
        """Index this discovery snapshot once; refresh creates a new catalog."""
        return {tool.name: tool for tool in self.tools}


type McpVariant = NoAuthMcp | BearerMcp | OAuthMcp
