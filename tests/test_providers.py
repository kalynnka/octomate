"""Unit tests for the multi-provider model registry.

Every provider here is constructed with dummy credentials so the SDK clients
build offline (no network, no ADC). Vertex is the exception — it eagerly loads
Application Default Credentials at construction — so it is covered only at the
config level.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

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
from octomate.providers import ProviderRegistry


def make_registry() -> ProviderRegistry:
    return ProviderRegistry(
        ProvidersConfig(
            openai=OpenAIProviderConfig(api_key=SecretStr("sk-test")),
            deepseek=DeepSeekProviderConfig(api_key=SecretStr("ds-test")),
            gemini=GeminiProviderConfig(api_key=SecretStr("g-test")),
            anthropic=AnthropicProviderConfig(api_key=SecretStr("sk-ant-test")),
            bedrock=BedrockProviderConfig(
                region_name="us-east-1",
                aws_access_key_id=SecretStr("AKIA-test"),
                aws_secret_access_key=SecretStr("secret-test"),
            ),
        )
    )


def test_invalid_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelConfig(provider="nope", name="x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("provider", "name", "model_cls"),
    [
        ("openai", "gpt-5.2", OpenAIChatModel),
        ("deepseek", "deepseek-chat", OpenAIChatModel),
        ("gemini", "gemini-3-flash-preview", GoogleModel),
        ("anthropic", "claude-sonnet-4-6", AnthropicModel),
        ("bedrock", "us.anthropic.claude-3-5-sonnet-20240620-v1:0", BedrockConverseModel),
    ],
)
def test_build_model_dispatches_to_provider(
    provider: str, name: str, model_cls: type
) -> None:
    model = make_registry().build_model(ModelConfig(provider=provider, name=name))  # type: ignore[arg-type]
    assert isinstance(model, model_cls)
    assert model.model_name == name


def test_bedrock_model_name_with_colon_is_preserved() -> None:
    name = "us.anthropic.claude-3-5-sonnet-20240620-v1:0"
    model = make_registry().build_model(ModelConfig(provider="bedrock", name=name))
    assert model.model_name == name


def test_anthropic_cache_breakpoints_are_default() -> None:
    model = make_registry().build_model(
        ModelConfig(provider="anthropic", name="claude-sonnet-4-6")
    )
    assert model.settings == {
        "anthropic_cache_tool_definitions": True,
        "anthropic_cache_instructions": True,
        "anthropic_cache": True,
    }


def test_bedrock_cache_breakpoints_are_default() -> None:
    model = make_registry().build_model(
        ModelConfig(provider="bedrock", name="us.anthropic.claude-3-5-sonnet-20240620-v1:0")
    )
    assert model.settings == {
        "bedrock_cache_tool_definitions": True,
        "bedrock_cache_instructions": True,
        "bedrock_cache_messages": True,
    }


def test_openai_cache_retention_is_default() -> None:
    model = make_registry().build_model(ModelConfig(provider="openai", name="gpt-5.2"))
    assert model.settings == {"openai_prompt_cache_retention": "24h"}


def test_deepseek_has_no_cache_settings() -> None:
    model = make_registry().build_model(
        ModelConfig(provider="deepseek", name="deepseek-chat")
    )
    assert not model.settings


def test_per_model_settings_merge_with_provider_defaults() -> None:
    model = make_registry().build_model(
        ModelConfig(
            provider="anthropic",
            name="claude-sonnet-4-6",
            settings={"temperature": 0.5, "max_tokens": 1024},
        )
    )
    settings = dict(model.settings or {})
    assert settings["temperature"] == 0.5  # per-model setting applied
    assert settings["max_tokens"] == 1024
    assert settings["anthropic_cache_instructions"] is True  # provider default kept
    # Unset settings don't leak through.
    assert "top_p" not in settings


def test_unknown_model_setting_is_dropped() -> None:
    # ModelConfig.settings is a pydantic-ai ModelSettings TypedDict, so keys that
    # aren't declared ModelSettings fields are dropped during validation.
    model = make_registry().build_model(
        ModelConfig.model_validate(
            {
                "provider": "openai",
                "name": "gpt-5.2",
                "settings": {"temperature": 0.5, "not_a_real_setting": 1},
            }
        )
    )
    settings = dict(model.settings or {})
    assert settings["temperature"] == 0.5
    assert "not_a_real_setting" not in settings
    assert settings["openai_prompt_cache_retention"] == "24h"  # provider default kept


def test_model_settings_thinking_reaches_built_model() -> None:
    # `thinking` is a per-model setting (the inkling default lives in model.settings);
    # it is baked into the built model by the registry.
    model = make_registry().build_model(
        ModelConfig(provider="openai", name="gpt-5.2", settings={"thinking": "medium"})
    )
    assert dict(model.settings or {})["thinking"] == "medium"


def test_providers_are_built_once_and_cached() -> None:
    registry = make_registry()
    assert registry.provider_for("openai") is registry.provider_for("openai")


def test_vertex_config_defaults_to_global_location() -> None:
    # Vertex needs ADC to construct, so assert the config default that preserves
    # the prior GoogleProvider(location="global") behavior instead of building.
    assert VertexProviderConfig().location == "global"
