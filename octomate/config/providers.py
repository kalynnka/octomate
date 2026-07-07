from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from octomate.config.models import (
    AnthropicModelSettings,
    BedrockModelSettings,
    OpenAIModelSettings,
)


class OpenAIProviderConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_key: SecretStr | None = None
    base_url: str | None = None
    settings: OpenAIModelSettings = OpenAIModelSettings(
        openai_prompt_cache_retention="24h",
    )


class DeepSeekProviderConfig(BaseModel):
    api_key: SecretStr | None = None


class GeminiProviderConfig(BaseModel):
    """Gemini via the Google Developer API (AI Studio)."""

    api_key: SecretStr | None = None


class VertexProviderConfig(BaseModel):
    """Gemini via Vertex AI. `location` defaults to "global" (matching the prior
    GoogleProvider(location="global")); `credentials_file` is a service-account
    JSON path, and when omitted Application Default Credentials are used."""

    project: str | None = None
    location: str | None = "global"
    credentials_file: str | None = None


class AnthropicProviderConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_key: SecretStr | None = None
    base_url: str | None = None
    settings: AnthropicModelSettings = AnthropicModelSettings(
        anthropic_cache_tool_definitions=True,
        anthropic_cache_instructions=True,
        anthropic_cache=True,
    )


class BedrockProviderConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    region_name: str | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    profile_name: str | None = None
    api_key: SecretStr | None = None
    settings: BedrockModelSettings = BedrockModelSettings(
        bedrock_cache_tool_definitions=True,
        bedrock_cache_instructions=True,
        bedrock_cache_messages=True,
    )


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    openai: OpenAIProviderConfig | None = None
    deepseek: DeepSeekProviderConfig | None = None
    gemini: GeminiProviderConfig | None = None
    vertex: VertexProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    bedrock: BedrockProviderConfig | None = None
