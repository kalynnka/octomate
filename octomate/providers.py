from __future__ import annotations

from typing import Any

from pydantic import SecretStr
from pydantic_ai.models import Model, parse_model_id
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
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
        self.providers: dict[str, Provider[Any]] = {}

    def build_model(self, model: ModelConfig) -> Model:
        provider_name = model.provider
        if provider_name is None:
            if model.name == "test":
                return TestModel(settings=model.settings)
            raise ValueError(
                f"unsupported model without provider prefix {model.name!r}"
            )
        provider = self.providers.get(provider_name)
        if provider is None:
            provider = self.build_provider(provider_name)
            self.providers[provider_name] = provider
        _, model_name = parse_model_id(model.name)
        # Provider defaults are the base; the per-model `settings` overrides them.
        merged = merge_model_settings(
            self.provider_defaults(provider_name), model.settings
        )
        match provider_name:
            case "openai" | "openai-chat" | "deepseek":
                return OpenAIChatModel(model_name, provider=provider, settings=merged)
            case "google" | "google-cloud":
                return GoogleModel(model_name, provider=provider, settings=merged)
            case "anthropic":
                return AnthropicModel(model_name, provider=provider, settings=merged)
            case "bedrock":
                return BedrockConverseModel(
                    model_name, provider=provider, settings=merged
                )
        raise ValueError(f"unsupported model provider {provider_name!r}")

    def provider_defaults(self, name: str) -> ModelSettings:
        """The provider's default settings (e.g. prompt caching) taken from the
        octomate config — defined as field defaults on the provider configs, so
        an absent provider block still contributes them. Providers without
        provider-specific settings contribute nothing."""
        match name:
            case "openai" | "openai-chat":
                return (self.config.openai or OpenAIProviderConfig()).settings
            case "anthropic":
                return (self.config.anthropic or AnthropicProviderConfig()).settings
            case "bedrock":
                return (self.config.bedrock or BedrockProviderConfig()).settings
            case _:
                return {}

    def build_provider(self, name: str) -> Provider[Any]:
        match name:
            case "openai" | "openai-chat":
                cfg = self.config.openai or OpenAIProviderConfig()
                return OpenAIProvider(
                    **present(api_key=cfg.api_key, base_url=cfg.base_url)
                )
            case "deepseek":
                cfg = self.config.deepseek or DeepSeekProviderConfig()
                return DeepSeekProvider(**present(api_key=cfg.api_key))
            case "google":
                cfg = self.config.gemini or GeminiProviderConfig()
                return GoogleProvider(**present(api_key=cfg.api_key))
            case "google-cloud":
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
        raise ValueError(f"unsupported model provider {name!r}")
