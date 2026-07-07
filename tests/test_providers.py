"""Unit tests for the multi-provider model registry.

Every provider here is constructed with dummy credentials so the SDK clients
build offline (no network, no ADC). Vertex is the exception — it eagerly loads
Application Default Credentials at construction — so it is covered only at the
config level.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.models import KnownModelName, parse_model_id
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel

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
from octomate.providers import ProviderHttpLogFilter, ProviderRegistry


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


def test_unsupported_provider_prefix_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported model provider prefix"):
        ModelConfig(name="xai:grok-3")


def test_model_without_provider_uses_pydantic_ai_default() -> None:
    config = ModelConfig(name="test", settings={"temperature": 0.5})
    model = make_registry().build_model(config)
    assert config.provider is None
    assert isinstance(model, TestModel)
    assert model.model_name == "test"
    assert model.settings == {"temperature": 0.5}


@pytest.mark.parametrize(
    ("name", "provider", "model_cls"),
    [
        ("openai:gpt-5.2", "openai", OpenAIChatModel),
        ("openai-chat:gpt-5.2", "openai-chat", OpenAIChatModel),
        ("deepseek:deepseek-chat", "deepseek", OpenAIChatModel),
        ("google:gemini-3-flash-preview", "google", GoogleModel),
        ("anthropic:claude-sonnet-4-6", "anthropic", AnthropicModel),
        (
            "bedrock:us.anthropic.claude-3-5-sonnet-20240620-v1:0",
            "bedrock",
            BedrockConverseModel,
        ),
    ],
)
def test_build_model_dispatches_to_provider(
    name: KnownModelName, provider: str, model_cls: type
) -> None:
    config = ModelConfig(name=name)
    model = make_registry().build_model(config)
    assert config.provider == provider
    assert isinstance(model, model_cls)
    assert model.model_name == parse_model_id(name)[1]


def test_bedrock_model_name_with_colon_is_preserved() -> None:
    name = "bedrock:us.anthropic.claude-3-5-sonnet-20240620-v1:0"
    model = make_registry().build_model(ModelConfig(name=name))
    assert model.model_name == parse_model_id(name)[1]


def test_anthropic_cache_breakpoints_are_default() -> None:
    model = make_registry().build_model(
        ModelConfig(name="anthropic:claude-sonnet-4-6")
    )
    assert model.settings == {
        "anthropic_cache_tool_definitions": True,
        "anthropic_cache_instructions": True,
        "anthropic_cache": True,
    }


def test_bedrock_cache_breakpoints_are_default() -> None:
    model = make_registry().build_model(
        ModelConfig(
            name="bedrock:us.anthropic.claude-3-5-sonnet-20240620-v1:0",
        )
    )
    assert model.settings == {
        "bedrock_cache_tool_definitions": True,
        "bedrock_cache_instructions": True,
        "bedrock_cache_messages": True,
    }


def test_openai_cache_retention_is_default() -> None:
    model = make_registry().build_model(
        ModelConfig(name="openai:gpt-5.2")
    )
    assert model.settings == {"openai_prompt_cache_retention": "24h"}


def test_deepseek_has_no_cache_settings() -> None:
    model = make_registry().build_model(
        ModelConfig(name="deepseek:deepseek-chat")
    )
    assert not model.settings


def test_per_model_settings_merge_with_provider_defaults() -> None:
    model = make_registry().build_model(
        ModelConfig(
            name="anthropic:claude-sonnet-4-6",
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
                "name": "openai:gpt-5.2",
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
        ModelConfig(
            name="openai:gpt-5.2",
            settings={"thinking": "medium"},
        )
    )
    assert dict(model.settings or {})["thinking"] == "medium"


def test_providers_are_built_once_and_cached() -> None:
    registry = make_registry()
    registry.build_model(ModelConfig(name="openai:gpt-5.2"))
    provider = registry.providers["openai"]
    registry.build_model(ModelConfig(name="openai:gpt-5.2"))
    assert registry.providers["openai"] is provider


def test_vertex_config_defaults_to_global_location() -> None:
    # Vertex needs ADC to construct, so assert the config default that preserves
    # the prior GoogleProvider(location="global") behavior instead of building.
    assert VertexProviderConfig().location == "global"


def test_provider_hosts_from_built_providers() -> None:
    registry = make_registry()
    assert registry.provider_hosts == set()  # nothing built yet
    registry.build_model(ModelConfig(name="openai:gpt-5.2"))
    registry.build_model(ModelConfig(name="deepseek:deepseek-chat"))
    assert registry.provider_hosts == {"api.openai.com", "api.deepseek.com"}


def httpx_request_record(level: int, method: str, url: str) -> logging.LogRecord:
    # Mirrors httpx's own request log call so the filter reads the URL from args[1].
    return logging.LogRecord(
        "httpx",
        level,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        (method, httpx.URL(url), "HTTP/1.1", 200, "OK"),
        None,
    )


def test_http_log_filter_keeps_providers_and_drops_others() -> None:
    registry = make_registry()
    registry.build_model(ModelConfig(name="deepseek:deepseek-chat"))
    log_filter = ProviderHttpLogFilter(registry)

    provider_call = httpx_request_record(
        logging.INFO, "POST", "https://api.deepseek.com/v1/chat/completions"
    )
    lark_call = httpx_request_record(
        logging.INFO, "PUT", "https://open.feishu.cn/open-apis/cardkit/v1/cards/x"
    )
    lark_warning = httpx_request_record(
        logging.WARNING, "PUT", "https://open.feishu.cn/open-apis/cardkit/v1/cards/x"
    )
    no_args = logging.LogRecord("httpx", logging.INFO, __file__, 1, "msg", None, None)

    assert log_filter.filter(provider_call) is True
    assert log_filter.filter(lark_call) is False
    assert log_filter.filter(lark_warning) is True  # warnings always pass
    assert log_filter.filter(no_args) is False  # unparseable request records drop
