from __future__ import annotations

from functools import cached_property
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_ai.models import KnownModelName, parse_model_id
from pydantic_ai.settings import ModelSettings

supported_providers: set[str] = {
    "openai",
    "openai-chat",
    "deepseek",
    "google",
    "google-cloud",
    "anthropic",
    "bedrock",
}

CacheTTL = bool | Literal["5m", "1h"]


class OpenAIModelSettings(ModelSettings, total=False):
    """Default OpenAI settings applied to every model this provider builds.
    Subclasses pydantic-ai's ModelSettings so it merges with no conversion; only
    declared ModelSettings keys are accepted (unknown keys are dropped)."""

    openai_prompt_cache_retention: Literal["in_memory", "24h"]


class AnthropicModelSettings(ModelSettings, total=False):
    """Default Anthropic settings applied to every model this provider builds."""

    anthropic_cache_tool_definitions: CacheTTL
    anthropic_cache_instructions: CacheTTL
    anthropic_cache: CacheTTL


class BedrockThinkingConfig(TypedDict, total=False):
    type: Literal["adaptive", "enabled", "disabled"]
    # Opus 4.7+ defaults to "omitted": thinking blocks stream with empty text
    # (signature only). "summarized" restores visible thinking deltas.
    display: Literal["summarized", "omitted"]


class BedrockOutputConfig(TypedDict, total=False):
    effort: Literal["low", "medium", "high"]


class BedrockAdditionalModelRequestFields(TypedDict, total=False):
    """Raw fields passed through to the Bedrock Converse API. Used for reasoning
    config on models whose thinking shape isn't covered by the unified
    `thinking` setting (e.g. claude-opus-4-x, which needs `type: adaptive`)."""

    thinking: BedrockThinkingConfig
    output_config: BedrockOutputConfig


class BedrockModelSettings(ModelSettings, total=False):
    """Default Bedrock settings applied to every model this provider builds."""

    bedrock_cache_tool_definitions: CacheTTL
    bedrock_cache_instructions: CacheTTL
    bedrock_cache_messages: CacheTTL
    bedrock_additional_model_requests_fields: BedrockAdditionalModelRequestFields


class ModelConfig(BaseModel):
    """A model selection: which provider builds it and the model name. Reusable
    across agents. `settings` is an optional per-model override (a pydantic-ai
    ModelSettings) layered under the agent and run settings; arbitrary_types is
    needed because ModelSettings embeds non-pydantic types (e.g. httpx.Timeout)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: KnownModelName
    settings: ModelSettings | None = None

    @cached_property
    def provider(self) -> str | None:
        provider, _ = parse_model_id(self.name)
        return provider

    @model_validator(mode="after")
    def validate_supported_provider(self) -> Self:
        if self.provider is not None and self.provider not in supported_providers:
            raise ValueError(f"unsupported model provider prefix in {self.name!r}")
        return self
