from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AuthConfig(BaseModel):
    """Deployment-owned credentials and token settings."""

    model_config = ConfigDict(hide_input_in_errors=True)

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

    api_key_prefix: str = Field(
        default="omk_", description="Prefix prepended to newly issued API keys."
    )
    api_key_salt: SecretStr = Field(
        min_length=16, description="Secret appended before hashing personal API keys."
    )
