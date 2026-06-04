from __future__ import annotations

from typing import Any

from pydantic import SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers import Provider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings, merge_model_settings

from octomate.config import (
    AnthropicProviderConfig,
    BedrockProviderConfig,
    DeepSeekProviderConfig,
    GeminiProviderConfig,
    ModelConfig,
    OpenAIProviderConfig,
    ProviderName,
    ProvidersConfig,
    VertexProviderConfig,
)


def present(**pairs: SecretStr | str | object | None) -> dict[str, Any]:
    """Build provider kwargs: drop None values (so each provider's own env-var
    fallback still applies per field — an unset api_key falls back to
    OPENAI_API_KEY, an unset region to AWS_DEFAULT_REGION) and unwrap any
    SecretStr to its plain value."""
    return {
        key: value.get_secret_value() if isinstance(value, SecretStr) else value
        for key, value in pairs.items()
        if value is not None
    }


class ProviderRegistry:
    """Builds pydantic-ai `Model`s from a `ModelConfig` using credentials in
    `ProvidersConfig`, falling back to each provider's native env vars for any
    unset field. One pydantic-ai `Provider` (and its SDK client) is constructed
    per provider key and reused across models."""

    def __init__(self, config: ProvidersConfig) -> None:
        self.config = config
        self.providers: dict[ProviderName, Provider[Any]] = {}

    def build_model(self, model: ModelConfig) -> Model:
        provider = self.provider_for(model.provider)
        # Provider defaults are the base; the per-model `settings` overrides them.
        merged = merge_model_settings(
            self.provider_defaults(model.provider), model.settings
        )
        match model.provider:
            case "openai" | "deepseek":
                return OpenAIChatModel(model.name, provider=provider, settings=merged)
            case "gemini" | "vertex":
                return GoogleModel(model.name, provider=provider, settings=merged)
            case "anthropic":
                return AnthropicModel(model.name, provider=provider, settings=merged)
            case "bedrock":
                return BedrockConverseModel(
                    model.name, provider=provider, settings=merged
                )

    def provider_defaults(self, name: ProviderName) -> ModelSettings:
        """The provider's default settings (e.g. prompt caching) taken from the
        octomate config — defined as field defaults on the provider configs, so
        an absent provider block still contributes them. Providers without
        provider-specific settings contribute nothing."""
        match name:
            case "openai":
                return (self.config.openai or OpenAIProviderConfig()).settings
            case "anthropic":
                return (self.config.anthropic or AnthropicProviderConfig()).settings
            case "bedrock":
                return (self.config.bedrock or BedrockProviderConfig()).settings
            case _:
                return {}

    def provider_for(self, name: ProviderName) -> Provider[Any]:
        cached = self.providers.get(name)
        if cached is not None:
            return cached
        provider = self.build_provider(name)
        self.providers[name] = provider
        return provider

    def build_provider(self, name: ProviderName) -> Provider[Any]:
        match name:
            case "openai":
                cfg = self.config.openai or OpenAIProviderConfig()
                return OpenAIProvider(
                    **present(api_key=cfg.api_key, base_url=cfg.base_url)
                )
            case "deepseek":
                cfg = self.config.deepseek or DeepSeekProviderConfig()
                return DeepSeekProvider(**present(api_key=cfg.api_key))
            case "gemini":
                cfg = self.config.gemini or GeminiProviderConfig()
                return GoogleProvider(vertexai=False, **present(api_key=cfg.api_key))
            case "vertex":
                cfg = self.config.vertex or VertexProviderConfig()
                credentials = None
                if cfg.credentials_file is not None:
                    # Imported here: only the service-account path needs it.
                    from google.oauth2 import service_account

                    credentials = service_account.Credentials.from_service_account_file(
                        cfg.credentials_file
                    )
                return GoogleProvider(
                    vertexai=True,
                    **present(
                        project=cfg.project,
                        location=cfg.location,
                        credentials=credentials,
                    ),
                )
            case "anthropic":
                cfg = self.config.anthropic or AnthropicProviderConfig()
                return AnthropicProvider(
                    **present(api_key=cfg.api_key, base_url=cfg.base_url)
                )
            case "bedrock":
                cfg = self.config.bedrock or BedrockProviderConfig()
                return BedrockProvider(
                    **present(
                        region_name=cfg.region_name,
                        aws_access_key_id=cfg.aws_access_key_id,
                        aws_secret_access_key=cfg.aws_secret_access_key,
                        aws_session_token=cfg.aws_session_token,
                        profile_name=cfg.profile_name,
                        api_key=cfg.api_key,
                    )
                )
