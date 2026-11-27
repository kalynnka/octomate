"""Local account credentials and token lifetimes."""

from datetime import timedelta

from mcp.server.auth.routes import validate_issuer_url
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)


class AuthConfig(BaseModel):
    """Deployment-owned credentials and token settings."""

    model_config = ConfigDict(hide_input_in_errors=True)

    invitation_lifetime: timedelta = Field(
        default=timedelta(days=7),
        gt=timedelta(0),
        description="Lifetime of an anonymous, single-use registration invitation.",
    )

    access_token_salt: SecretStr = Field(
        min_length=16, description="Secret appended before hashing access tokens."
    )
    access_token_lifetime: timedelta = Field(
        default=timedelta(minutes=15),
        gt=timedelta(0),
        description="Lifetime of an issued access token.",
    )

    refresh_token_salt: SecretStr = Field(
        min_length=16, description="Secret appended before hashing refresh tokens."
    )
    session_lifetime: timedelta = Field(
        default=timedelta(days=15),
        gt=timedelta(0),
        description="Absolute session lifetime; refresh does not extend it.",
    )
    cookie_secure: bool = Field(
        default=True, description="Require HTTPS for browser session cookies."
    )
    oauth_authorization_lifetime: timedelta = Field(
        default=timedelta(minutes=5),
        gt=timedelta(0),
        le=timedelta(minutes=10),
        description="Lifetime of pending OAuth consent and, after approval, its single-use code.",
    )
    oauth_issuer_url: AnyHttpUrl | None = Field(
        default=None,
        description="Public origin for inbound OAuth. Enables native-harness and CLI authorization; HTTPS except for local development.",
    )

    oauth_ui_url: AnyHttpUrl | None = Field(
        default=None,
        description="Trunkline origin for OAuth sign-in and consent; defaults to the issuer origin when Trunkline is served there.",
    )

    @field_validator("oauth_issuer_url", "oauth_ui_url")
    @classmethod
    def validate_oauth_issuer(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None:
            validate_issuer_url(value)
            if value.path not in {None, "/"} or value.username or value.password:
                raise ValueError("OAuth origins must not contain a path or credentials")
        return value

    oauth_grant_idle_lifetime: timedelta = Field(
        default=timedelta(days=30),
        gt=timedelta(0),
        description="Idle lifetime of native-harness and CLI OAuth grants, renewed by successful token refresh; no absolute lifetime.",
    )

    api_key_prefix: str = Field(
        default="omk_", description="Prefix prepended to newly issued API keys."
    )
    api_key_salt: SecretStr = Field(
        min_length=16, description="Secret appended before hashing personal API keys."
    )
    runtime_api_key_lifetime: timedelta = Field(
        default=timedelta(days=1),
        gt=timedelta(0),
        description="Idle lifetime of a runtime API key, extended by acquisition and successful authentication.",
    )
