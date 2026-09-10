"""The shapes every MCP tentacle's config shares: the server and the name its
tools are listed under, with either one operator credential or a person's own
linked account behind it."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator

from octomate.types.oauth import HttpsUrl


class McpConfig(BaseModel):
    """What every MCP tentacle is configured with: the server, and the name its
    tools are listed under."""

    type: str
    enabled: bool = True


class BareMcpConfig(McpConfig):
    """A vendor's MCP server spoken to with one operator credential for every
    caller: the deployment's identity, not the person's."""

    type: Literal["bare"] = "bare"
    url: str
    token: SecretStr | None = None


class DeviceFlowConfig(BaseModel):
    type: Literal["device"] = "device"
    device_authorization_endpoint: HttpsUrl
    token_endpoint: HttpsUrl


class AuthorizationCodeFlowConfig(BaseModel):
    type: Literal["authorization_code"] = "authorization_code"
    authorization_endpoint: HttpsUrl
    token_endpoint: HttpsUrl


class OAuthMcpConfig(McpConfig):
    """A configured OAuth application whose grants belong to individual users."""

    type: Literal["oauth"] = "oauth"
    url: HttpsUrl
    client_id: str = Field(min_length=1)
    client_secret: SecretStr | None = None
    token_endpoint_auth_method: Literal[
        "none", "client_secret_basic", "client_secret_post"
    ] = "none"
    scopes: list[str] = Field(
        default_factory=list,
        description="Access requested when connecting. Widening it requires reauthorization.",
    )
    scope_separator: Literal[" ", ","] = Field(
        default=" ",
        description="Delimiter used in the provider's token response scopes.",
    )
    invalid_credentials_errors: list[str] = Field(
        default_factory=lambda: ["invalid_grant", "invalid_client"],
        description="OAuth error codes that require reconnecting instead of retrying a refresh.",
    )
    flow: Annotated[
        DeviceFlowConfig | AuthorizationCodeFlowConfig, Field(discriminator="type")
    ]

    @model_validator(mode="after")
    def validate_client_authentication(self) -> Self:
        if self.token_endpoint_auth_method == "none":
            if self.client_secret is not None:
                raise ValueError(
                    "client_secret requires a token endpoint authentication method"
                )
        elif self.client_secret is None or not self.client_secret.get_secret_value():
            raise ValueError(
                "the token endpoint authentication method requires client_secret"
            )
        return self
