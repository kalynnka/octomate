from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderOAuthConnectionConfig(BaseModel):
    kind: Literal["provider"] = "provider"
    provider: str


class McpOAuthConnectionConfig(BaseModel):
    kind: Literal["mcp"] = "mcp"
    resource_url: AnyHttpUrl


OAuthConnectionConfig: TypeAlias = Annotated[
    ProviderOAuthConnectionConfig | McpOAuthConnectionConfig,
    Field(discriminator="kind"),
]


class OAuthConfig(BaseModel):
    connections: dict[str, OAuthConnectionConfig] = Field(default_factory=dict)


class OAuthSecuritySettings(BaseSettings):
    """Environment-only keys for encrypting user OAuth secrets at rest."""

    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_OAUTH_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    primary_key_id: str = ""
    encryption_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_primary_key(self) -> OAuthSecuritySettings:
        if self.primary_key_id not in self.encryption_keys:
            raise ValueError("primary_key_id must name one of encryption_keys")
        return self
