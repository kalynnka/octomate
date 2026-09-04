from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import get_args

import pytest
from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import SecretStr, ValidationError
from pydantic_ai.settings import ThinkingEffort

from octomate.config import (
    AgentModelConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    DeepseekConfig,
    GitHubIntegrationConfig,
    InklingConfig,
    LarkChannelConfig,
    LinearIntegrationConfig,
    McpServerConfig,
    ModelConfig,
    NapcatChannelConfig,
    OctomateConfig,
    SlackChannelConfig,
    UserConfig,
)
from octomate.config.base import CONFIG_FILES, DEFAULTS_DIR, config_home
from octomate.config.channels import SLACK_MCP_SCOPES
from octomate.config.database import DatabaseSettings, database_settings
from octomate.config.observability import LogfireConfig
from octomate.schemas.project import DirectoryUpstream, Project
from octomate.schemas.triage import Claim
from tests.support.agents import CLAUDE_MODELS, CODEX_MODELS, DEEPSEEK_MODELS
from tests.support.config import ISOLATED_HOME

IN_MEMORY_DB_URL = "sqlite+aiosqlite:///:memory:"


def test_the_suite_never_reads_the_developers_config() -> None:
    """`./.octomate/` and `~/.octomate/` are gitignored, so anything they carry — a
    user, half a channel's secrets — would make a result depend on the machine. The
    session fixture points `OCTOMATE_HOME` at `tests/config/` and clears the
    environment; this is what notices if either half stops."""

    assert config_home() == ISOLATED_HOME
    # `OCTOMATE_DB_URL` is the harness's own and stays; only the home joins it.
    assert sorted(name for name in os.environ if name.startswith("OCTOMATE")) == [
        "OCTOMATE_DB_URL",
        "OCTOMATE_HOME",
    ]

    live = OctomateConfig()
    assert live.users == {}


def test_an_explicit_home_wins_over_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`OCTOMATE_HOME` is obeyed as given, without probing for a config file.

    That is what lets it isolate absolutely — the suite's own home declares nothing,
    and an explicit home that had to prove itself first would silently fall through
    to `./.octomate/` and read the developer's deployment instead.
    """
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))
    assert config_home() == tmp_path
    assert list(tmp_path.iterdir()) == []
    assert OctomateConfig().channels == {}


def test_a_home_is_discovered_only_when_it_holds_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent `OCTOMATE_HOME`, the project's `./.octomate/config/` is preferred over
    the machine's `~/.octomate/config/` — but only once it holds config. Every
    checkout has a `./.octomate/` for the database and `cli.toml`, and neither of
    those must make it shadow the machine's deployment."""
    user_home = tmp_path / "home" / ".octomate" / "config"
    user_home.mkdir(parents=True)
    (user_home / "octomate.yaml").write_text("port: 9001\n")
    project_home = tmp_path / "project" / ".octomate" / "config"
    project_home.mkdir(parents=True)
    (tmp_path / "project" / ".octomate" / "cli.toml").write_text(
        'url = "http://127.0.0.1:8000"\n'
    )

    monkeypatch.delenv("OCTOMATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "project")

    # Data-only, so the machine's home still wins.
    assert config_home() == user_home
    assert OctomateConfig().port == 9001

    # One config file is enough to claim it, and it replaces rather than merges.
    (project_home / "octomate.yaml").write_text("host: 0.0.0.0\n")
    assert config_home() == project_home
    config = OctomateConfig()
    assert str(config.host) == "0.0.0.0"
    assert config.port == 8000


def test_the_suite_never_points_at_the_real_database() -> None:
    """`database_settings` is built when its module is imported, before any fixture
    exists, so pyproject's `env` is the only thing that can move it. A test that
    forgets `in_memory_engine` must reach memory, not `.octomate/octomate.db`."""

    assert database_settings.db_url == IN_MEMORY_DB_URL
    # And one built during the run, which is what the singleton alone would miss.
    assert DatabaseSettings().db_url == IN_MEMORY_DB_URL


def test_the_packaged_defaults_are_a_valid_deployment() -> None:
    """The defaults under `octomate/config/defaults/` are the floor beneath every
    config home, so they have to validate on their own — a home that declares only
    `channels:` still loads the rest of them. They used to ship `~` placeholders that
    could not, which only went unnoticed because a deployment replaced the file
    whole; the suite runs on them now, which is what keeps them honest."""

    assert set(CONFIG_FILES) == {path.name for path in DEFAULTS_DIR.glob("*.yaml")}

    config = OctomateConfig()
    # Nothing is turned on: no agent, no channel, no provider. Which LLM an
    # operator holds keys for is not guessable, so the defaults decline to guess.
    assert config.agents.configured_models() == {}
    assert config.channels == {}
    assert config.providers.deepseek is None


def test_channel_config_parses_supported_channels() -> None:
    config = OctomateConfig.model_validate(
        {
            "agents": {
                "inkling": {"models": [{"name": "openai:gpt-4o"}]},
            },
            "channels": {
                "slack": {
                    "type": "slack",
                    "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                },
                "lark": {
                    "type": "lark",
                    "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
                    "app_id": "cli-test",
                    "app_secret": "secret",
                },
                "napcat": {
                    "type": "napcat",
                    "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
                    "ws_url": "ws://127.0.0.1:3001",
                    "http_url": "http://127.0.0.1:3000",
                },
            },
        }
    )

    assert isinstance(config.channels["slack"], SlackChannelConfig)
    assert isinstance(config.channels["lark"], LarkChannelConfig)
    assert isinstance(config.channels["napcat"], NapcatChannelConfig)
    assert config.channels["slack"].stream.flush_interval == 0.2
    assert config.channels["slack"].stream.min_chars == 20
    assert config.channels["lark"].stream.flush_interval == 0.2
    assert config.channels["lark"].stream.min_chars == 20
    assert config.channels["napcat"].stream.enabled is False


def test_inkling_request_limit_defaults_to_256() -> None:
    assert (
        InklingConfig(models=[ModelConfig(name="openai:gpt-4o")]).request_limit == 256
    )


def test_inkling_request_limit_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        InklingConfig.model_validate(
            {
                "models": [{"name": "deepseek:deepseek-v4-pro"}],
                "request_limit": 0,
            }
        )


def test_channel_config_parses_agent_model_routes() -> None:
    config = OctomateConfig.model_validate(
        {
            "agents": {
                "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                "claude": {"models": ["opus"]},
                "codex": {"models": ["gpt-5.3-codex"]},
            },
            "channels": {
                "slack": {
                    "type": "slack",
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "agents": [
                        {"agent": "inkling", "model": "openai:gpt-4o"},
                        {"agent": "claude", "model": "opus"},
                        {"agent": "codex", "model": "gpt-5.3-codex"},
                    ],
                },
            },
        }
    )

    assert config.channels["slack"] is not None
    assert config.channels["slack"].agents == [
        AgentModelConfig(agent="inkling", model="openai:gpt-4o"),
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
    config = ClaudeCodeConfig(models=set(CLAUDE_MODELS))

    assert config.models == {"haiku", "sonnet[1m]", "opus[1m]", "opusplan[1m]"}


def test_claude_code_config_validates_model_names() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        ClaudeCodeConfig.model_validate({"models": {"missing"}})


def test_claude_code_config_warns_a_remote_host_is_not_honoured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Remote runs are off while a run's directory is its thread's workspace, and
    # nothing can make one on the host at the other end. The block is kept as
    # written — the transport that would have read it is what is parked.
    with caplog.at_level("WARNING"):
        config = ClaudeCodeConfig(
            models=set(CLAUDE_MODELS), ssh=ClaudeSSHConfig(host="user@box")
        )

    assert config.ssh is not None
    assert "user@box" in caplog.text
    assert "stays local" in caplog.text


def test_claude_code_config_accepts_documented_model_aliases() -> None:
    config = ClaudeCodeConfig(models={"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"})

    assert config.models == {"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"}


def test_codex_config_defaults_to_current_model_set() -> None:
    config = CodexConfig(models=set(CODEX_MODELS))

    assert config.models == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.3-codex",
        "gpt-5.1-codex-mini",
    }
    assert config.permission_mode == "user_review"


def test_codex_config_rejects_stale_model_aliases() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        CodexConfig.model_validate({"models": {"gpt-5-codex"}})


def test_codex_config_parses_sdk_runtime_config() -> None:
    config = CodexConfig.model_validate(
        {
            "models": ["gpt-5.5"],
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
            },
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
            "models": ["gpt-5.5"],
            "permission_mode": "auto_review",
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

    assert config.permission_mode == "auto_review"
    assert config.sandbox == "read_only"
    assert config.base_instructions == "stay concise"
    assert config.developer_instructions == "work carefully"
    assert config.ephemeral is True
    assert config.model_provider == "openai"
    assert config.personality == "pragmatic"
    assert config.effort == "xhigh"
    assert config.summary == "detailed"

    denied = CodexConfig.model_validate(
        {"models": ["gpt-5.5"], "permission_mode": "deny_all"}
    )
    assert denied.permission_mode == "deny_all"
    # The sandbox keeps its own default; no posture moves it.
    assert denied.sandbox == "workspace_write"


def test_codex_config_validates_sdk_setting_names() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        CodexConfig.model_validate(
            {
                "permission_mode": "never",
                "sandbox": "workspace-write",
                "effort": "extreme",
                "summary": "verbose",
                "personality": "spicy",
            }
        )


def test_deepseek_config_defaults_to_the_shipped_shape() -> None:
    config = DeepseekConfig(models=set(DEEPSEEK_MODELS))

    assert config.models == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert config.permission_mode == "workspace-write"
    assert config.provider == "deepseek-official"
    assert config.executable == "dsh"
    # dsh's own default bind, so an ordinary `dsh web` is attached to as-is.
    assert (config.host, config.port) == ("127.0.0.1", 3080)
    # dsh's own default home, expanded like any configured value.
    assert config.dsh_home == Path("~/.dsh").expanduser()
    # Octomate's one effort scale lands on the llm-deepseek adapter's ids.
    assert config.efforts == {
        "minimal": "off",
        "low": "off",
        "medium": "high",
        "high": "high",
        "xhigh": "max",
    }


def test_deepseek_config_rejects_unknown_model_labels() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        DeepseekConfig.model_validate({"models": {"deepseek-v3"}})


def test_deepseek_config_rejects_a_foreign_permission_preset() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        DeepseekConfig.model_validate({"permission_mode": "user_review"})


def test_deepseek_config_rejects_a_non_loopback_host() -> None:
    # The /api gateway has no auth and a started child binds loopback, so a
    # remote host is refused at load rather than failing at attach time.
    with pytest.raises(ValidationError, match="Input should be"):
        DeepseekConfig.model_validate({"host": "dsh.example"})


def test_channel_agent_routes_must_reference_configured_agent() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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
                },
            }
        )

    [error] = exc_info.value.errors()
    assert error["type"] == "channel_agent_route"
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_agent_route_validation_reports_all_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {"models": ["opus"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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
                        "type": "lark",
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
        OctomateConfig.model_validate(
            {
                "channels": {
                    "slack": {
                        "type": "slack",
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
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": None,
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {"models": ["opus"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "claude"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    # A discriminated union stamps the resolved tag into the path, so a field error
    # inside a channel carries its `type` between the key and the field.
    assert error["loc"] == ("channels", "slack", "slack", "agents", 0, "model")
    assert error["msg"] == "Field required"


def test_channel_claude_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {"models": ["opus"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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


def test_channel_claude_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {"enabled": False, "models": ["opus"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "claude", "model": "sonnet"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_codex_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "codex": {"models": ["gpt-5.3-codex"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "codex": {"enabled": False, "models": ["gpt-5.5"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
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


def test_channel_deepseek_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "deepseek": {"models": ["deepseek-v4-flash"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "deepseek", "model": "deepseek-v4-pro"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "model")
    assert (
        error["msg"] == "'deepseek-v4-pro' is not configured in agents.deepseek.models"
    )


def test_channel_deepseek_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "deepseek": {"enabled": False, "models": ["deepseek-v4-pro"]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "deepseek", "model": "deepseek-v4-pro"}],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0, "agent")
    assert error["msg"] == "'deepseek' does not match a configured agent tentacle"


def test_channel_inkling_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {"inkling": {"models": [{"name": "openai:gpt-4o"}]}},
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [{"agent": "inkling", "model": "openai:gpt-5.2"}],
                    }
                },
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

    config = OctomateConfig.model_validate(
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
    config = OctomateConfig.model_validate(
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

    config = OctomateConfig.model_validate(
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
    config = OctomateConfig.model_validate(
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

    config = OctomateConfig.model_validate(
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

    config = OctomateConfig()

    assert config.integrations["github"] is not None
    assert config.integrations["github"].client_id == "Iv1.env"
    assert config.oauth.encryption_key is not None


def test_channel_stream_config_uses_partial_defaults_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agents.yaml").write_text(
        "agents:\n  inkling:\n    models:\n      - name: openai:gpt-4o\n",
        encoding="utf-8",
    )
    (tmp_path / "channels.yaml").write_text(
        """
channels:
  slack:
    type: slack
    agents: [{agent: inkling, model: "openai:gpt-4o"}]
    app_id: A-test
    bot_token: xoxb-test
    app_token: xapp-test
    stream:
      enabled: false
  lark:
    type: lark
    agents: [{agent: inkling, model: "openai:gpt-4o"}]
    app_id: cli-test
    app_secret: secret
    stream:
      enabled: false
  napcat:
    type: napcat
    agents: [{agent: inkling, model: "openai:gpt-4o"}]
    ws_url: ws://127.0.0.1:3001
    http_url: http://127.0.0.1:3000
    stream:
      flush_interval: 0.3
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))

    config = OctomateConfig()

    assert config.channels["slack"] is not None
    assert config.channels["slack"].stream.enabled is False
    assert config.channels["slack"].stream.flush_interval == 0.2
    assert config.channels["slack"].stream.min_chars == 20

    assert config.channels["lark"] is not None
    assert config.channels["lark"].stream.enabled is False
    assert config.channels["lark"].stream.flush_interval == 0.2
    assert config.channels["lark"].stream.min_chars == 20

    assert config.channels["napcat"] is not None
    assert config.channels["napcat"].stream.enabled is False
    assert config.channels["napcat"].stream.flush_interval == 0.3


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
            "models": ["gpt-5.5"],
            "claims": {
                "gpt-5.5": {
                    "ability": "Deep repository work in the acme monorepo.",
                    "efforts": ["low", "medium", "high"],
                }
            },
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
        OctomateConfig.model_validate(
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


def test_user_links_refuse_a_native_pseudo_channel() -> None:
    # The runtime claim retired: a native session is registered by the user's
    # own `secret`, so a pseudo-channel link has no claimed row left to seed and
    # is as unresolvable as any typo — declared runtime or not.
    with pytest.raises(
        ValidationError, match="'claude-native' does not match a configured channel"
    ):
        OctomateConfig.model_validate(
            {
                "agents": {"claude": {"models": ["opus"]}},
                "users": {
                    "luhui": {
                        "profiles": {"claude-native": {"channel_user_id": "native"}}
                    }
                },
            }
        )


def test_distinct_user_secrets_validate() -> None:
    config = OctomateConfig.model_validate(
        {"users": {"lu": {"secret": "lu-token"}, "hui": {"secret": "hui-token"}}}
    )

    lu_secret = config.users["lu"].secret
    assert lu_secret is not None
    assert lu_secret.get_secret_value() == "lu-token"
    # A user with no secret stays valid: registration is opt-in per user.
    assert OctomateConfig.model_validate({"users": {"lu": {}}}).users["lu"].secret is (
        None
    )


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


def test_projects_validate_as_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared project is a `Project` — no config model restating it — so `~` expands
    # and the block's key is the name, whatever the row would otherwise be called.
    monkeypatch.setenv("HOME", str(tmp_path))

    config = OctomateConfig.model_validate(
        {
            "projects": {
                "octomate": {
                    "root": "~/Projects/inky",
                    "extra_roots": ["~/Library/Code"],
                    "description": "Octomate itself.",
                    "upstream": {"kind": "directory", "path": "~/Projects/inky"},
                }
            }
        }
    )

    [declared] = config.projects.values()
    project = Project.shell(declared)
    assert project.root == tmp_path / "Projects" / "inky"
    assert project.extra_roots == [tmp_path / "Library" / "Code"]
    assert project.upstream == DirectoryUpstream(path=tmp_path / "Projects" / "inky")


def test_a_mirrors_block_validates() -> None:
    config = OctomateConfig.model_validate(
        {
            "mirrors": {
                "freshness_window": 300,
                "identity": {"name": "Lu Hui", "email": "lu@example.com"},
            }
        }
    )

    assert config.mirrors.freshness_window == 300
    assert config.mirrors.identity.name == "Lu Hui"


def test_a_workspaces_block_validates() -> None:
    config = OctomateConfig.model_validate(
        {"workspaces": {"idle_window": 3600, "sweep_interval": 600}}
    )

    assert config.workspaces.idle_window == 3600
    assert config.workspaces.sweep_interval == 600


def test_a_workspaces_block_defaults_to_a_day_and_an_hour() -> None:
    config = OctomateConfig()

    assert config.workspaces.idle_window == 24 * 60 * 60
    assert config.workspaces.sweep_interval == 60 * 60


def test_a_sweep_that_never_runs_is_refused() -> None:
    # A zero interval is a busy loop and a zero window reclaims a workspace the
    # moment a turn ends, which is the fork paid for on every single turn.
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate({"workspaces": {"sweep_interval": 0}})
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate({"workspaces": {"idle_window": 0}})


def test_a_project_without_a_root_is_refused() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate({"projects": {"inky": {"description": "?"}}})

    [error] = exc_info.value.errors()
    assert "inky.root: Field required" in error["msg"]


def test_a_projects_block_error_says_what_the_block_held() -> None:
    # The rest of the config hides its inputs because they are credentials; a list
    # where the mapping belongs would otherwise fail as a bare `dict_type`, and the
    # one block with nothing to hide is the one that most needs to show itself.
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate({"projects": [{"root": "~/Projects/inky"}]})

    [error] = exc_info.value.errors()
    assert "the block: Input should be a valid dictionary" in error["msg"]
    assert "[{'root': '~/Projects/inky'}]" in error["msg"]


def test_user_links_accept_configured_channels() -> None:
    config = OctomateConfig.model_validate(
        {
            "agents": {
                "inkling": {"models": [{"name": "openai:gpt-4o"}]},
            },
            "channels": {
                "napcat": {
                    "type": "napcat",
                    "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
                    "ws_url": "ws://x",
                    "http_url": "http://x",
                },
                "trunkline": {
                    "type": "trunkline",
                    "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
                },
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


def test_one_vendor_can_be_mounted_once_per_account() -> None:
    # The key is the connector id, so two Linears differ by name rather than by
    # anything the config has to invent.
    config = OctomateConfig.model_validate(
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
        OctomateConfig.model_validate(
            {"integrations": {"linear_home": {"client_id": "lin_b"}}}
        )


def slack_channel_block(**overrides: object) -> dict[str, object]:
    return {
        "type": "slack",
        "agents": [{"agent": "inkling", "model": "openai:gpt-4o"}],
        "app_id": "A-test",
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        **overrides,
    }


def test_a_slack_channel_offering_its_tools_needs_the_apps_oauth_client() -> None:
    with pytest.raises(ValidationError, match="needs an `oauth` block"):
        OctomateConfig.model_validate(
            {
                "agents": {"inkling": {"models": [{"name": "openai:gpt-4o"}]}},
                "channels": {"slack": slack_channel_block(mcp=True)},
            }
        )


def test_a_slack_oauth_client_stores_tokens_and_so_needs_the_key() -> None:
    deployment = {
        "agents": {"inkling": {"models": [{"name": "openai:gpt-4o"}]}},
        "channels": {
            "slack": slack_channel_block(
                mcp=True, oauth={"client_id": "1.2", "client_secret": "shh"}
            )
        },
    }

    with pytest.raises(
        ValidationError,
        match=r"oauth\.encryption_key is required when channels\.slack\.oauth",
    ):
        OctomateConfig.model_validate(deployment)

    config = OctomateConfig.model_validate(
        {
            **deployment,
            "oauth": {"encryption_key": urlsafe_b64encode(bytes(range(32))).decode()},
        }
    )
    slack = config.channels["slack"]
    assert isinstance(slack, SlackChannelConfig)
    assert slack.oauth is not None
    # What the forwarded tools need and nothing that posts as the person.
    assert slack.oauth.scopes == SLACK_MCP_SCOPES
    assert "chat:write" not in slack.oauth.scopes
    assert str(slack.oauth.callback_base_uri) == "http://localhost:8000/"
