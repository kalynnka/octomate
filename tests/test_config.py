from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import SecretStr, ValidationError
from pydantic_ai.settings import ThinkingEffort
from pydantic_settings import SettingsConfigDict

from octomate.config import (
    AgentModelConfig,
    ChannelsConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    GitHubIntegrationConfig,
    LarkChannelConfig,
    LinearIntegrationConfig,
    McpServerConfig,
    NapcatChannelConfig,
    OctomateConfig,
    SlackChannelConfig,
    UserConfig,
)
from octomate.config.observability import LogfireConfig
from octomate.schemas.triage import Claim
from tests.support.config import IsolatedTestConfig


class DefaultYamlOnlyConfig(OctomateConfig):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_",
        env_nested_delimiter="__",
        yaml_file=("octomate.default.yaml",),
        yaml_config_section="octomate",
        nested_model_default_partial_update=True,
        extra="ignore",
    )


def test_default_yaml_placeholders_fail_without_override() -> None:
    # octomate.default.yaml ships `~` placeholders that must be overridden in
    # octomate.yaml or via env; loading the defaults file alone fails fast with
    # clear validation errors rather than silently falling back to code defaults.
    with pytest.raises(ValidationError):
        DefaultYamlOnlyConfig()


def test_channels_default_to_none() -> None:
    channels = ChannelsConfig()
    assert channels.slack is None
    assert channels.lark is None
    assert channels.napcat is None
    assert channels.trunkline is not None
    assert channels.trunkline.agents == [
        AgentModelConfig(model="deepseek:deepseek-v4-pro")
    ]


def test_channel_config_parses_supported_channels() -> None:
    config = IsolatedTestConfig.model_validate(
        {
            "channels": {
                "trunkline": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                },
                "lark": {
                    "app_id": "cli-test",
                    "app_secret": "secret",
                },
                "napcat": {
                    "ws_url": "ws://127.0.0.1:3001",
                    "http_url": "http://127.0.0.1:3000",
                },
            }
        }
    )

    assert isinstance(config.channels.slack, SlackChannelConfig)
    assert isinstance(config.channels.lark, LarkChannelConfig)
    assert isinstance(config.channels.napcat, NapcatChannelConfig)
    assert config.channels.slack.stream.flush_interval == 0.2
    assert config.channels.slack.stream.min_chars == 20
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 20
    assert config.channels.napcat.stream.enabled is False


def test_channel_config_parses_agent_model_routes() -> None:
    config = IsolatedTestConfig.model_validate(
        {
            "agents": {
                "claude": {"models": ["opus"]},
                "codex": {"models": ["gpt-5.3-codex"]},
            },
            "channels": {
                "trunkline": None,
                "lark": None,
                "napcat": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "agents": [
                        {"agent": "inkling", "model": "deepseek:deepseek-v4-pro"},
                        {"agent": "claude", "model": "opus"},
                        {"agent": "codex", "model": "gpt-5.3-codex"},
                    ],
                },
            },
        }
    )

    assert config.channels.slack is not None
    assert config.channels.slack.agents == [
        AgentModelConfig(agent="inkling", model="deepseek:deepseek-v4-pro"),
        AgentModelConfig(agent="claude", model="opus"),
        AgentModelConfig(agent="codex", model="gpt-5.3-codex"),
    ]


def test_claude_code_config_uses_model_route_mapping() -> None:
    config = ClaudeCodeConfig.model_validate(
        {
            "models": ["opus", "sonnet"],
        }
    )

    assert config.models == {"opus", "sonnet"}
    assert not hasattr(config, "model")


def test_claude_code_config_requires_model_mapping() -> None:
    with pytest.raises(ValidationError):
        ClaudeCodeConfig.model_validate(
            {
                "models": [
                    "opus",
                    {"opus": "claude-opus-4-8"},
                ]
            }
        )


def test_claude_code_config_defaults_to_fixed_model_set() -> None:
    config = ClaudeCodeConfig()

    assert config.models == {"haiku", "sonnet[1m]", "opus[1m]", "opusplan[1m]"}


def test_claude_code_config_validates_model_names() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        ClaudeCodeConfig.model_validate({"models": {"missing"}})


def test_claude_code_config_refuses_a_remote_host() -> None:
    # Remote runs are off while a run's directory is its thread's project root: the
    # root is a local path, and the host on the other end has nothing to match it.
    with pytest.raises(ValidationError, match="remote runs are disabled"):
        ClaudeCodeConfig(ssh=ClaudeSSHConfig(host="user@box"))


def test_claude_code_config_accepts_documented_model_aliases() -> None:
    config = ClaudeCodeConfig(models={"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"})

    assert config.models == {"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"}


def test_codex_config_defaults_to_current_model_set() -> None:
    config = CodexConfig()

    assert config.models == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.3-codex",
        "gpt-5.1-codex-mini",
    }
    assert config.approval_mode == "user"


def test_codex_config_rejects_stale_model_aliases() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        CodexConfig.model_validate({"models": {"gpt-5-codex"}})


def test_codex_config_parses_sdk_runtime_config() -> None:
    config = CodexConfig.model_validate(
        {
            "runtime": {
                "codex_bin": "/opt/codex",
                "launch_args_override": [
                    "codex",
                    "app-server",
                    "--listen",
                    "stdio://",
                ],
                "config_overrides": ["model_provider=openai"],
                "cwd": "/repo",
                "env": {"CODEX_HOME": "/tmp/codex"},
                "client_name": "octomate-test",
                "client_title": "Octomate Test",
                "client_version": "test-version",
                "experimental_api": False,
            }
        }
    )

    assert isinstance(config.runtime, CodexSdkConfig)
    assert config.runtime.codex_bin == "/opt/codex"
    assert config.runtime.launch_args_override == (
        "codex",
        "app-server",
        "--listen",
        "stdio://",
    )
    assert config.runtime.config_overrides == ("model_provider=openai",)
    assert config.runtime.cwd == "/repo"
    assert config.runtime.env == {"CODEX_HOME": "/tmp/codex"}
    assert config.runtime.client_name == "octomate-test"
    assert config.runtime.client_title == "Octomate Test"
    assert config.runtime.client_version == "test-version"
    assert config.runtime.experimental_api is False


def test_codex_config_accepts_sdk_thread_and_turn_settings() -> None:
    config = CodexConfig.model_validate(
        {
            "approval_mode": "auto_review",
            "sandbox": "read_only",
            "base_instructions": "stay concise",
            "developer_instructions": "work carefully",
            "ephemeral": True,
            "model_provider": "openai",
            "personality": "pragmatic",
            "effort": "xhigh",
            "summary": "detailed",
        }
    )

    assert config.approval_mode == "auto_review"
    assert config.sandbox == "read_only"
    assert config.base_instructions == "stay concise"
    assert config.developer_instructions == "work carefully"
    assert config.ephemeral is True
    assert config.model_provider == "openai"
    assert config.personality == "pragmatic"
    assert config.effort == "xhigh"
    assert config.summary == "detailed"

    user_config = CodexConfig.model_validate({"approval_mode": "user"})
    assert user_config.approval_mode == "user"


def test_codex_config_validates_sdk_setting_names() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        CodexConfig.model_validate(
            {
                "approval_mode": "never",
                "sandbox": "workspace-write",
                "effort": "extreme",
                "summary": "verbose",
                "personality": "spicy",
            }
        )


def test_channel_agent_routes_must_reference_configured_agent() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "channels": {
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [
                            {
                                "agent": "ghost",
                                "model": "deepseek:deepseek-v4-flash",
                            }
                        ],
                    }
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["type"] == "channel_agent_route"
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_agent_route_validation_reports_all_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"claude": {"models": ["opus"]}},
                "channels": {
                    "trunkline": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [
                            {"agent": "ghost", "model": "deepseek:deepseek-v4-flash"},
                            {"agent": "inkling", "model": "openai:gpt-5.2"},
                            {"agent": "claude", "model": "sonnet"},
                        ],
                    },
                    "lark": {
                        "enabled": False,
                        "app_id": "cli-test",
                        "app_secret": "secret",
                        "agents": [
                            {"agent": "nobody", "model": "deepseek:deepseek-v4-flash"},
                        ],
                    },
                },
            },
        )

    errors = {tuple(error["loc"]): error["msg"] for error in exc_info.value.errors()}
    assert errors == {
        (
            "channels",
            "slack",
            "agents",
            0,
            "agent",
        ): "'ghost' does not match a configured agent tentacle",
        (
            "channels",
            "slack",
            "agents",
            1,
            "model",
        ): "'openai:gpt-5.2' is not configured in agents.inkling.models",
        (
            "channels",
            "slack",
            "agents",
            2,
            "model",
        ): "'sonnet' is not configured in agents.claude.models",
        (
            "channels",
            "lark",
            "agents",
            0,
            "agent",
        ): "'nobody' does not match a configured agent tentacle",
    }


def test_disabled_channel_agent_routes_are_validated() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "channels": {
                    "slack": {
                        "enabled": False,
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [
                            {
                                "agent": "ghost",
                                "model": "deepseek:deepseek-v4-flash",
                            }
                        ],
                    }
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_claude_route_requires_claude_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"claude": None},
                "channels": {
                    "trunkline": None,
                    "lark": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "claude", "model": "opus"}],
                    },
                },
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_routes_require_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"claude": {"models": ["opus"]}},
                "channels": {
                    "trunkline": None,
                    "lark": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "claude"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "model")
    assert error["msg"] == "Field required"


def test_channel_claude_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"claude": {"models": ["opus"]}},
                "channels": {
                    "trunkline": None,
                    "lark": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "claude", "model": "sonnet"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "model")
    assert error["msg"] == "'sonnet' is not configured in agents.claude.models"


def test_channel_codex_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"codex": {"models": ["gpt-5.3-codex"]}},
                "channels": {
                    "trunkline": None,
                    "lark": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "codex", "model": "gpt-5.5"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "model")
    assert error["msg"] == "'gpt-5.5' is not configured in agents.codex.models"


def test_channel_codex_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "agents": {"codex": {"enabled": False}},
                "channels": {
                    "trunkline": None,
                    "lark": None,
                    "napcat": None,
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "codex", "model": "gpt-5.3-codex"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'codex' does not match a configured agent tentacle"


def test_channel_inkling_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "channels": {
                    "slack": {
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "inkling", "model": "openai:gpt-5.2"}],
                    }
                }
            }
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "model")
    assert error["msg"] == "'openai:gpt-5.2' is not configured in agents.inkling.models"


def test_each_connection_carries_its_own_warm_timeout() -> None:
    # Both reuse the shared McpConfig, so the warm timeout is the same field, defaulting
    # to 16s with no general fallback.
    assert (
        McpServerConfig(
            url="https://mcp.linear.app/mcp", token=SecretStr("lin_x")
        ).warm_timeout_seconds
        == 16.0
    )
    assert (
        GitHubIntegrationConfig(client_id="Iv1.test").mcp.warm_timeout_seconds == 16.0
    )

    config = IsolatedTestConfig.model_validate(
        {
            "mcp": {
                "linear": {
                    "url": "https://mcp.linear.app/mcp",
                    "token": "lin_x",
                    "warm_timeout_seconds": 3.0,
                },
            },
            "integrations": {
                "github": {
                    "type": "github",
                    "client_id": "Iv1.test",
                    "mcp": {"warm_timeout_seconds": 7.0},
                },
            },
        }
    )
    assert config.mcp["linear"].warm_timeout_seconds == 3.0
    assert config.integrations["github"] is not None
    assert config.integrations["github"].mcp.warm_timeout_seconds == 7.0
    # A partial mcp override still keeps the GitHub endpoint default.
    assert config.integrations["github"].mcp.url == "https://api.githubcopilot.com/mcp/"


def test_github_integration_rejects_a_scope_github_does_not_define() -> None:
    # GitHub ignores a scope it does not recognise and returns a token quietly
    # missing that access, so a typo has to fail here instead.
    config = IsolatedTestConfig.model_validate(
        {
            "integrations": {
                "github": {
                    "type": "github",
                    "client_id": "Iv1.test",
                    "scopes": ["repo", "workflow"],
                }
            },
            "oauth": {"encryption_key": "x" * 43 + "="},
        }
    )
    assert config.integrations["github"] is not None
    assert config.integrations["github"].scopes == ["repo", "workflow"]

    # Validated, not constructed: a scope arrives as untyped YAML.
    with pytest.raises(ValidationError, match="Input should be"):
        GitHubIntegrationConfig.model_validate(
            {"client_id": "Iv1.test", "scopes": ["workfl0w"]}
        )


def test_github_integration_cache_size_default_and_override() -> None:
    assert GitHubIntegrationConfig(client_id="Iv1.test").max_cached_users == 32

    config = IsolatedTestConfig.model_validate(
        {
            "integrations": {
                "github": {
                    "type": "github",
                    "client_id": "Iv1.test",
                    "max_cached_users": 8,
                }
            }
        }
    )
    assert config.integrations["github"] is not None
    assert config.integrations["github"].max_cached_users == 8


def test_config_parses_integrations_and_mcp_servers() -> None:
    config = IsolatedTestConfig.model_validate(
        {
            "integrations": {
                "github": {
                    "type": "github",
                    "enabled": True,
                    "client_id": "Iv1.test",
                    "scopes": ["repo", "read:org"],
                    "mcp": {"read_only": True},
                },
            },
            "mcp": {
                "linear": {"url": "https://mcp.linear.app/mcp", "token": "lin_test"},
            },
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )

    assert isinstance(config.integrations["github"], GitHubIntegrationConfig)
    assert config.integrations["github"].enabled is True
    assert config.integrations["github"].mcp.read_only is True
    assert config.integrations["github"].client_id == "Iv1.test"
    assert config.integrations["github"].scopes == ["repo", "read:org"]
    assert config.integrations["github"].mcp.url == "https://api.githubcopilot.com/mcp/"

    linear = config.mcp["linear"]
    assert linear.prefix is None
    assert linear.enabled is True
    assert linear.url == "https://mcp.linear.app/mcp"


def test_mcp_server_token_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Structure in YAML, secret in the environment: the key names the env var.
    monkeypatch.setenv("OCTOMATE__MCP__LINEAR__TOKEN", "lin_from_env")

    config = IsolatedTestConfig.model_validate(
        {"mcp": {"linear": {"url": "https://mcp.linear.app/mcp"}}}
    )

    assert config.mcp["linear"].token.get_secret_value() == "lin_from_env"


def test_github_oauth_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # `type` too: it is the discriminator, so without it the block resolves to no
    # provider at all — the local octomate.yaml used to supply it by accident.
    monkeypatch.setenv("OCTOMATE__INTEGRATIONS__GITHUB__TYPE", "github")
    monkeypatch.setenv("OCTOMATE__INTEGRATIONS__GITHUB__ENABLED", "true")
    monkeypatch.setenv("OCTOMATE__INTEGRATIONS__GITHUB__CLIENT_ID", "Iv1.env")
    monkeypatch.setenv(
        "OCTOMATE__OAUTH__ENCRYPTION_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )

    config = IsolatedTestConfig()

    assert config.integrations["github"] is not None
    assert config.integrations["github"].client_id == "Iv1.env"
    assert config.oauth.encryption_key is not None


def test_channel_stream_config_uses_partial_defaults_from_yaml(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "octomate.yaml"
    config_file.write_text(
        """
octomate:
  channels:
    slack:
      app_id: A-test
      bot_token: xoxb-test
      app_token: xapp-test
      stream:
        enabled: false
    lark:
      app_id: cli-test
      app_secret: secret
      stream:
        enabled: false
    napcat:
      ws_url: ws://127.0.0.1:3001
      http_url: http://127.0.0.1:3000
      stream:
        flush_interval: 0.3
""",
        encoding="utf-8",
    )

    class PartialYamlConfig(OctomateConfig):
        model_config = SettingsConfigDict(
            env_prefix="OCTOMATE_",
            env_nested_delimiter="__",
            yaml_file=(config_file,),
            yaml_config_section="octomate",
            nested_model_default_partial_update=True,
            extra="ignore",
        )

    config = PartialYamlConfig()

    assert config.channels.slack is not None
    assert config.channels.slack.stream.enabled is False
    assert config.channels.slack.stream.flush_interval == 0.2
    assert config.channels.slack.stream.min_chars == 20

    assert config.channels.lark is not None
    assert config.channels.lark.stream.enabled is False
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 20

    assert config.channels.napcat is not None
    assert config.channels.napcat.stream.enabled is False
    assert config.channels.napcat.stream.flush_interval == 0.3


def test_logfire_instrumentation_defaults_off() -> None:
    """Library auto-instrumentation is opt-in per library: a fresh config traces
    nothing but Octomate's own spans. A default flipping on here silently turns
    every HTTP request / SQL statement / model call into span volume."""
    instrument = LogfireConfig().instrument
    assert not instrument.pydantic_ai
    assert not instrument.httpx
    assert not instrument.sqlalchemy


def test_claim_efforts_default_matches_pydantic_ais_thinking_scale() -> None:
    # The default is written out rather than derived from `get_args`, so a
    # pydantic-ai release that adds or drops a grade has to be looked at per route
    # instead of silently widening every claim that omits `efforts`.
    assert Claim(ability="anything").efforts == get_args(ThinkingEffort)


def test_agent_claims_override_parses_from_config() -> None:
    config = CodexConfig.model_validate(
        {
            "claims": {
                "gpt-5.5": {
                    "ability": "Deep repository work in the acme monorepo.",
                    "efforts": ["low", "medium", "high"],
                }
            }
        }
    )

    assert config.claims == {
        "gpt-5.5": Claim(
            ability="Deep repository work in the acme monorepo.",
            efforts=("low", "medium", "high"),
        )
    }


def test_user_links_must_reference_configured_channel() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "users": {
                    "luhui": {
                        "name": "Lu",
                        "profiles": {"matrix": {"channel_user_id": "@lu:x"}},
                    }
                }
            }
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("users", "luhui", "profiles", "matrix")
    assert error["msg"] == "'matrix' does not match a configured channel"


def test_user_profile_config_rejects_the_old_user_id_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserConfig.model_validate(
            {
                "profiles": {
                    "slack": {
                        "channel_user_id": "U1",
                        "user_id": "U1",
                        "name": "Lu",
                    }
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("profiles", "slack", "user_id")
    assert error["type"] == "uuid_parsing"


def test_user_profile_config_ignores_server_generated_id() -> None:
    supplied_id = "00000000-0000-0000-0000-000000000001"

    config = UserConfig.model_validate(
        {"profiles": {"slack": {"channel_user_id": "U1", "id": supplied_id}}}
    )

    assert str(config.profiles["slack"].id) != supplied_id


def test_user_profile_config_requires_channel_user_id_in_a_mapping() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserConfig.model_validate({"profiles": {"slack": {"name": "Lu"}}})

    [error] = exc_info.value.errors()
    assert error["loc"] == ("profiles", "slack")
    assert "channel_user_id is required in a YAML profile" in error["msg"]


def test_user_profile_config_rejects_scalar_id_shorthand() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserConfig.model_validate({"profiles": {"slack": "U1"}})

    [error] = exc_info.value.errors()
    assert error["loc"] == ("profiles", "slack")
    assert error["type"] == "model_attributes_type"


def test_user_links_accept_configured_channels() -> None:
    config = IsolatedTestConfig.model_validate(
        {
            "channels": {
                "napcat": {"ws_url": "ws://x", "http_url": "http://x"},
            },
            "users": {
                "luhui": {
                    "name": "Lu",
                    "profiles": {
                        "napcat": {"channel_user_id": "9"},
                        "trunkline": {"channel_user_id": "dev"},
                    },
                },
            },
        }
    )

    profiles = config.users["luhui"].profiles
    assert {key: profile.channel_user_id for key, profile in profiles.items()} == {
        "napcat": "9",
        "trunkline": "dev",
    }


def test_projects_validate_from_yaml_with_tilde_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared root has to exist, so `~` is pointed somewhere this test built
    # rather than at whatever the machine running it happens to have.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Projects/octoverse/inky").mkdir(parents=True)
    (tmp_path / "Library/Application Support/Code/User").mkdir(parents=True)

    config = IsolatedTestConfig.model_validate(
        {
            "projects": [
                {
                    "root": "~/Projects/octoverse/inky",
                    "extra_roots": ["~/Library/Application Support/Code/User"],
                    "description": "Octomate itself.",
                    "permission_mode": "accept_edits",
                }
            ]
        }
    )

    [inky] = config.projects
    assert inky.root == tmp_path / "Projects/octoverse/inky"
    assert inky.extra_roots == [tmp_path / "Library/Application Support/Code/User"]
    assert inky.description == "Octomate itself."
    assert inky.permission_mode == "accept_edits"
    # Unnamed, so it is called after its root's directory.
    assert inky.name == "inky"


def test_a_project_can_name_itself(tmp_path: Path) -> None:
    config = IsolatedTestConfig.model_validate(
        {"projects": [{"root": str(tmp_path), "name": "Inky"}]}
    )

    assert config.projects[0].name == "Inky"


def test_projects_load_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A list has no key to name one entry, so the override is the whole block as
    # JSON; `OCTOMATE__PROJECTS__0__ROOT` is not a form pydantic-settings accepts.
    monkeypatch.setenv(
        "OCTOMATE__PROJECTS",
        json.dumps([{"root": str(tmp_path), "permission_mode": "bypass_permissions"}]),
    )

    config = IsolatedTestConfig()

    [inky] = config.projects
    assert inky.root == tmp_path
    assert inky.permission_mode == "bypass_permissions"


@pytest.mark.parametrize(
    "root", ["file:///srv/inky", "file://minidock/srv/inky", "ssh://minidock/srv/inky"]
)
def test_a_project_root_written_as_a_url_is_refused(root: str) -> None:
    # `Path("file:///srv/inky").absolute()` is a real path under the cwd that matches
    # nothing, so a url has to be refused rather than quietly converted.
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate({"projects": [{"root": root}]})

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0, "root")
    assert "a root is a plain local path" in error["msg"]


def test_a_project_root_that_is_a_file_is_refused() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {"projects": [{"root": "octomate.default.yaml"}]}
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0)
    assert error["type"] == "project_root_not_a_directory"
    assert "is a file; a root is a directory" in error["msg"]


def test_a_project_root_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    # Declaring a project claims something about this machine, so a root that is not
    # there fails at config load rather than at the first cwd that should have
    # matched — and the error names the project, not just the path.
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {"projects": [{"root": str(tmp_path / "not-cloned-yet")}]}
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0)
    assert error["type"] == "project_root_missing"
    assert "project 'not-cloned-yet': root" in error["msg"]
    assert "does not exist on this machine" in error["msg"]


def test_a_project_extra_root_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate(
            {
                "projects": [
                    {"root": str(tmp_path), "extra_roots": [str(tmp_path / "gone")]}
                ]
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0)
    assert error["type"] == "project_root_missing"
    assert "extra root" in error["msg"]
    assert "does not exist on this machine" in error["msg"]


def test_the_filesystem_root_is_refused_as_a_project_root() -> None:
    # It is a directory and it exists, but it has no name to be called by — the one
    # case that would leave a project unnameable.
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate({"projects": [{"root": "/"}]})

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0, "root")
    assert "is the filesystem root" in error["msg"]


def test_a_relative_project_root_is_made_absolute() -> None:
    config = IsolatedTestConfig.model_validate({"projects": [{"root": "octomate"}]})

    assert config.projects[0].root == Path.cwd() / "octomate"


def test_a_project_without_a_root_is_refused() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IsolatedTestConfig.model_validate({"projects": [{"description": "no root"}]})

    [error] = exc_info.value.errors()
    assert error["loc"] == ("projects", 0, "root")
    assert error["type"] == "missing"


def test_one_vendor_can_be_mounted_once_per_account() -> None:
    # The key is the connector id, so two Linears differ by name rather than by
    # anything the config has to invent.
    config = IsolatedTestConfig.model_validate(
        {
            "integrations": {
                "linear_work": {"type": "linear", "client_id": "lin_a"},
                "linear_home": {
                    "type": "linear",
                    "client_id": "lin_b",
                    "callback_base_uri": "http://localhost:9000",
                },
            }
        }
    )

    work = config.integrations["linear_work"]
    home = config.integrations["linear_home"]
    assert isinstance(work, LinearIntegrationConfig)
    assert isinstance(home, LinearIntegrationConfig)
    assert (work.client_id, home.client_id) == ("lin_a", "lin_b")
    assert str(home.callback_base_uri) == "http://localhost:9000/"


def test_an_integration_without_a_type_is_refused() -> None:
    # Nothing else in the block says which provider builds it.
    with pytest.raises(ValidationError, match="tag"):
        IsolatedTestConfig.model_validate(
            {"integrations": {"linear_home": {"client_id": "lin_b"}}}
        )
