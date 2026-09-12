from __future__ import annotations

from datetime import timedelta

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from octomate.types.oauth import HttpsUrl


class OAuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token_refresh_leeway: timedelta = Field(
        default=timedelta(minutes=5),
        ge=timedelta(0),
        description="Refresh OAuth tokens this long before their expiry; numeric values are seconds.",
    )
    callback_base_uri: AnyHttpUrl | None = Field(
        default=None,
        description="Browser-reachable Octomate URL for dynamically installed MCP authorization callbacks.",
    )
    client_metadata_url: HttpsUrl | None = Field(
        default=None,
        description="Optional hosted OAuth client metadata document; use dynamic registration when absent or unsupported.",
    )

    encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "URL-safe base64 encoding of the 32-byte AES key used to encrypt OAuth "
            "operation secrets and user tokens at rest."
        ),
    )

    @field_validator("client_metadata_url")
    @classmethod
    def metadata_document(cls, value: HttpsUrl | None) -> HttpsUrl | None:
        if value is not None and (
            value.path in (None, "", "/")
            or value.username
            or value.password
            or value.query
            or value.fragment
        ):
            raise ValueError(
                "OAuth client metadata requires an HTTPS document URL without credentials, query, or fragment"
            )
        return value
