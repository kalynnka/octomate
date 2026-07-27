from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class OAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "URL-safe base64 encoding of the 32-byte AES key used to encrypt OAuth "
            "operation secrets and user tokens at rest."
        ),
    )
