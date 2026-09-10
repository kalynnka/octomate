from __future__ import annotations

import importlib
import os
from base64 import urlsafe_b64encode
from datetime import timedelta
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest
from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import ValidationError
from pydantic_ai.settings import ThinkingEffort

from octomate.config import (
    AgentModelConfig,
    BareMcpConfig,
    ChannelConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    DeepseekConfig,
    DiscordChannelConfig,
    DiscordStreamConfig,
    InklingConfig,
    LarkChannelConfig,
    ModelConfig,
    NapcatChannelConfig,
    OAuthMcpConfig,
    OctomateConfig,
    SlackChannelConfig,
)
from octomate.config.base import CONFIG_FILES, DEFAULTS_DIR, config_home
from octomate.config.channels import SLACK_MCP_SCOPES
from octomate.config.database import DatabaseSettings, database_settings
from octomate.config.observability import LogfireConfig
from octomate.schemas.project import DirectoryUpstream, Project
from octomate.schemas.triage import Claim
from tests.support.config import ISOLATED_HOME
from tests.support.mcp import configured_mcp

IN_MEMORY_DB_URL = "sqlite+aiosqlite:///:memory:"


def test_the_suite_never_reads_the_developers_config() -> None:
    """`./.octomate/` and `~/.octomate/` are gitignored, so anything they carry — half
    a channel's secrets — would make a result depend on the machine. The
    session fixture points `OCTOMATE_HOME` at `tests/config/` and clears the
    environment; this is what notices if either half stops."""

    assert config_home() == ISOLATED_HOME
    # `OCTOMATE_DB_URL` is the harness's own and stays; only the home joins it.
    assert sorted(name for name in os.environ if name.startswith("OCTOMATE")) == [
        "OCTOMATE_DB_URL",
        "OCTOMATE_HOME",
    ]

    live = OctomateConfig()
    assert live.tentacles == {}


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
    assert OctomateConfig().tentacles == {}


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
    assert config.tentacles == {}
    assert config.providers.deepseek is None
    assert config.auth is None


def test_tentacles_preserve_types_ids_and_serialization() -> None:
    config = OctomateConfig(
        tentacles={
            "coding": CodexConfig(),
            "review": ClaudeCodeConfig(enabled=False),
            "assistant": InklingConfig(models=[ModelConfig(name="openai:gpt-4o")]),
        }
    )
    serialized = config.model_dump(mode="json")
    assert list(serialized["tentacles"]) == ["coding", "review", "assistant"]
    assert serialized["tentacles"]["review"]["type"] == "claude"
    assert serialized["tentacles"]["review"]["enabled"] is False
    assert not {"agents", "channels", "mcp"} & serialized.keys()
    restored = OctomateConfig.model_validate_json(config.model_dump_json())
    assert restored.tentacles == config.tentacles


@pytest.mark.parametrize("enabled", [True, False])
def test_tentacles_reject_duplicate_agent_runtimes(enabled: bool) -> None:
    with pytest.raises(ValidationError, match="duplicate agent runtime 'codex'"):
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "coding": {"type": "codex"},
                    "review": {"type": "codex", "enabled": enabled},
                }
            }
        )


def test_channels_route_to_named_agent_tentacles() -> None:
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "coding": {"type": "codex"},
                "review": {"type": "claude", "enabled": False},
                "web": {"type": "trunkline", "agents": ["coding"]},
            }
        }
    )
    assert isinstance(config.tentacles["coding"], CodexConfig)
    web = config.tentacles["web"]
    assert isinstance(web, ChannelConfig)
    assert web.agents == ["coding"]


@pytest.mark.parametrize("target", ["missing", "web", "review"])
def test_channels_reject_missing_non_agent_or_disabled_routes(target: str) -> None:
    with pytest.raises(ValidationError, match="does not match a configured agent"):
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "review": {"type": "claude", "enabled": False},
                    "web": {"type": "trunkline", "agents": [target]},
                }
            }
        )


def test_channel_config_parses_supported_channels() -> None:
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "inkling": {"type": "inkling", "models": [{"name": "openai:gpt-4o"}]},
                "slack": {
                    "type": "slack",
                    "agents": ["inkling"],
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                },
                "lark": {
                    "type": "lark",
                    "agents": ["inkling"],
                    "app_id": "cli-test",
                    "app_secret": "secret",
                },
                "napcat": {
                    "type": "napcat",
                    "agents": ["inkling"],
                    "ws_url": "ws://127.0.0.1:3001",
                    "http_url": "http://127.0.0.1:3000",
                },
                "discord": {
                    "type": "discord",
                    "agents": ["inkling"],
                    "bot_token": "discord-test",
                },
            }
        }
    )

    assert isinstance(config.tentacles["slack"], SlackChannelConfig)
    assert isinstance(config.tentacles["lark"], LarkChannelConfig)
    assert isinstance(config.tentacles["napcat"], NapcatChannelConfig)
    assert isinstance(config.tentacles["discord"], DiscordChannelConfig)
    assert isinstance(config.tentacles["discord"].stream, DiscordStreamConfig)
    assert config.tentacles["slack"].stream.flush_interval == 0.2
    assert config.tentacles["slack"].stream.min_chars == 20
    assert config.tentacles["lark"].stream.flush_interval == 0.2
    assert config.tentacles["lark"].stream.min_chars == 20
    assert config.tentacles["napcat"].stream.enabled is False
    assert config.tentacles["discord"].stream.enabled is False
    assert config.tentacles["discord"].stream.flush_interval == 0.2


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


def test_channel_config_binds_agents_without_models() -> None:
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "inkling": {"type": "inkling", "models": [{"name": "openai:gpt-4o"}]},
                "claude": {
                    "type": "claude",
                },
                "codex": {
                    "type": "codex",
                },
                "slack": {
                    "type": "slack",
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "agents": [
                        "inkling",
                        "claude",
                        "codex",
                    ],
                },
            }
        }
    )

    assert isinstance(config.tentacles["slack"], SlackChannelConfig)
    assert config.tentacles["slack"].agents == [
        "inkling",
        "claude",
        "codex",
    ]


def test_claude_code_config_warns_a_remote_host_is_not_honoured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Remote runs are off while a run's directory is its thread's workspace, and
    # nothing can make one on the host at the other end. The block is kept as
    # written — the transport that would have read it is what is parked.
    with caplog.at_level("WARNING"):
        config = ClaudeCodeConfig(ssh=ClaudeSSHConfig(host="user@box"))

    assert config.ssh is not None
    assert "user@box" in caplog.text
    assert "stays local" in caplog.text


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
            "permission_mode": "auto_review",
            "sandbox": "read_only",
            "base_instructions": "stay concise",
            "developer_instructions": "work carefully",
            "ephemeral": True,
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
    assert config.personality == "pragmatic"
    assert config.effort == "xhigh"
    assert config.summary == "detailed"

    denied = CodexConfig.model_validate({"permission_mode": "deny_all"})
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
    config = DeepseekConfig()

    assert config.permission_mode == "workspace-write"
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
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["ghost"],
                    },
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["type"] == "channel_agent_route"
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_agent_route_validation_reports_all_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "claude": {
                        "type": "claude",
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": [
                            "ghost",
                            "inkling",
                            "claude",
                        ],
                    },
                    "lark": {
                        "type": "lark",
                        "enabled": False,
                        "app_id": "cli-test",
                        "app_secret": "secret",
                        "agents": [
                            "nobody",
                        ],
                    },
                }
            },
        )

    errors = {tuple(error["loc"]): error["msg"] for error in exc_info.value.errors()}
    assert errors == {
        (
            "tentacles",
            "slack",
            "agents",
            0,
        ): "'ghost' does not match a configured agent tentacle",
        (
            "tentacles",
            "lark",
            "agents",
            0,
        ): "'nobody' does not match a configured agent tentacle",
    }


def test_disabled_channel_agent_routes_are_validated() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "slack": {
                        "type": "slack",
                        "enabled": False,
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["ghost"],
                    }
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_claude_route_requires_claude_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["claude"],
                    },
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_claude_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "claude": {
                        "type": "claude",
                        "enabled": False,
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["claude"],
                    },
                }
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_codex_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "codex": {
                        "type": "codex",
                        "enabled": False,
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["codex"],
                    },
                }
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'codex' does not match a configured agent tentacle"


def test_channel_deepseek_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "deepseek": {
                        "type": "deepseek",
                        "enabled": False,
                    },
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["deepseek"],
                    },
                }
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("tentacles", "slack", "agents", 0)
    assert error["msg"] == "'deepseek' does not match a configured agent tentacle"


def test_oauth_mcp_accepts_provider_defined_scopes() -> None:
    config = configured_mcp().model_dump(mode="json")
    config["scopes"] = ["custom:read", "custom:write"]
    assert OAuthMcpConfig.model_validate(config).scopes == [
        "custom:read",
        "custom:write",
    ]


def test_config_parses_each_mcp_tentacle_type() -> None:
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "github": configured_mcp(client_id="Iv1.test"),
                "linear": {
                    "type": "bare",
                    "url": "https://mcp.linear.app/mcp",
                    "token": "lin_test",
                },
            },
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )

    github = config.tentacles["github"]
    assert isinstance(github, OAuthMcpConfig)
    assert github.enabled is True
    assert github.client_id == "Iv1.test"
    assert github.scopes == ["tools:read"]
    assert str(github.url) == "https://mcp.example/mcp"

    linear = config.tentacles["linear"]
    assert isinstance(linear, BareMcpConfig)
    assert linear.enabled is True
    assert linear.url == "https://mcp.linear.app/mcp"


def test_a_linked_account_needs_the_encryption_key() -> None:
    # The tokens it stores are what the key protects; a bare server stores none.
    OctomateConfig.model_validate(
        {
            "tentacles": {
                "notion": {
                    "type": "bare",
                    "url": "https://mcp.notion.com/mcp",
                    "token": "ntn_x",
                }
            }
        }
    )
    with pytest.raises(
        ValidationError,
        match=r"oauth\.encryption_key is required when tentacles\.github",
    ):
        OctomateConfig.model_validate(
            {"tentacles": {"github": configured_mcp(client_id="Iv1.test")}}
        )


def test_mcp_server_token_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Structure in YAML, secret in the environment: the key names the env var.
    monkeypatch.setenv("OCTOMATE__TENTACLES__LINEAR__TOKEN", "lin_from_env")

    config = OctomateConfig.model_validate(
        {"tentacles": {"linear": {"type": "bare", "url": "https://mcp.linear.app/mcp"}}}
    )

    linear = config.tentacles["linear"]
    assert isinstance(linear, BareMcpConfig)
    assert linear.token is not None
    assert linear.token.get_secret_value() == "lin_from_env"


def test_oauth_mcp_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # `type` too: it is the discriminator, so without it the block resolves to no
    # tentacle at all — the local octomate.yaml used to supply it by accident.
    monkeypatch.setenv("OCTOMATE__TENTACLES__GITHUB__TYPE", "oauth")
    monkeypatch.setenv("OCTOMATE__TENTACLES__GITHUB__URL", "https://mcp.example/mcp")
    monkeypatch.setenv(
        "OCTOMATE__TENTACLES__GITHUB__FLOWS",
        '[{"type": "device", "device_authorization_endpoint": "https://auth.example/device", "token_endpoint": "https://auth.example/token"}]',
    )
    monkeypatch.setenv("OCTOMATE__TENTACLES__GITHUB__CLIENT_ID", "Iv1.env")
    monkeypatch.setenv(
        "OCTOMATE__OAUTH__ENCRYPTION_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )

    config = OctomateConfig()

    github = config.tentacles["github"]
    assert isinstance(github, OAuthMcpConfig)
    assert github.client_id == "Iv1.env"
    assert config.oauth.encryption_key is not None


def test_channel_stream_config_uses_partial_defaults_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tentacles.yaml").write_text(
        """
tentacles:
  inkling:
    type: inkling
    models:
      - name: openai:gpt-4o
  slack:
    type: slack
    agents: [inkling]
    app_id: A-test
    bot_token: xoxb-test
    app_token: xapp-test
    stream:
      enabled: false
  lark:
    type: lark
    agents: [inkling]
    app_id: cli-test
    app_secret: secret
    stream:
      enabled: false
  napcat:
    type: napcat
    agents: [inkling]
    ws_url: ws://127.0.0.1:3001
    http_url: http://127.0.0.1:3000
    stream:
      flush_interval: 0.3
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))

    config = OctomateConfig()

    assert isinstance(config.tentacles["slack"], SlackChannelConfig)
    assert config.tentacles["slack"].stream.enabled is False
    assert config.tentacles["slack"].stream.flush_interval == 0.2
    assert config.tentacles["slack"].stream.min_chars == 20

    assert isinstance(config.tentacles["lark"], LarkChannelConfig)
    assert config.tentacles["lark"].stream.enabled is False
    assert config.tentacles["lark"].stream.flush_interval == 0.2
    assert config.tentacles["lark"].stream.min_chars == 20

    assert isinstance(config.tentacles["napcat"], NapcatChannelConfig)
    assert config.tentacles["napcat"].stream.enabled is False
    assert config.tentacles["napcat"].stream.flush_interval == 0.3


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
            },
        }
    )

    assert config.claims == {
        "gpt-5.5": Claim(
            ability="Deep repository work in the acme monorepo.",
            efforts=("low", "medium", "high"),
        )
    }


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


def test_mcp_pool_timeout_loads_from_yaml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))
    assert OctomateConfig().mcp_pool.idle_timeout == 3600
    (tmp_path / "tentacles.yaml").write_text("mcp_pool:\n  idle_timeout: 1200\n")
    assert OctomateConfig().mcp_pool.idle_timeout == 1200
    monkeypatch.setenv("OCTOMATE__MCP_POOL__IDLE_TIMEOUT", "600")
    assert OctomateConfig().mcp_pool.idle_timeout == 600


def test_oauth_refresh_leeway_loads_from_yaml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))
    assert OctomateConfig().oauth.token_refresh_leeway == timedelta(minutes=5)
    (tmp_path / "oauth.yaml").write_text("oauth:\n  token_refresh_leeway: 60\n")
    assert OctomateConfig().oauth.token_refresh_leeway == timedelta(seconds=60)
    monkeypatch.setenv("OCTOMATE__OAUTH__TOKEN_REFRESH_LEEWAY", "PT2M")
    assert OctomateConfig().oauth.token_refresh_leeway == timedelta(minutes=2)


def test_oauth_refresh_leeway_rejects_negative_durations() -> None:
    with pytest.raises(ValidationError, match="token_refresh_leeway"):
        OctomateConfig.model_validate({"oauth": {"token_refresh_leeway": -1}})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_mcp_pool_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError, match="idle_timeout"):
        OctomateConfig.model_validate({"mcp_pool": {"idle_timeout": timeout}})


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


def test_one_vendor_can_be_mounted_once_per_account() -> None:
    # The key is the connector id, so two GitHubs differ by name rather than by
    # anything the config has to invent.
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "github_work": configured_mcp(client_id="Iv1.a"),
                "github_home": configured_mcp(client_id="Iv1.b"),
            },
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )

    work = config.tentacles["github_work"]
    home = config.tentacles["github_home"]
    assert isinstance(work, OAuthMcpConfig)
    assert isinstance(home, OAuthMcpConfig)
    assert (work.client_id, home.client_id) == ("Iv1.a", "Iv1.b")


def test_an_mcp_block_without_a_type_is_refused() -> None:
    # Nothing else in the block says which tentacle builds it.
    with pytest.raises(ValidationError, match="tag"):
        OctomateConfig.model_validate(
            {"tentacles": {"linear_home": {"client_id": "lin_b"}}}
        )


def slack_channel_block(**overrides: object) -> dict[str, object]:
    return {
        "type": "slack",
        "agents": ["inkling"],
        "app_id": "A-test",
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        **overrides,
    }


def test_a_slack_channel_offering_its_tools_needs_the_apps_oauth_client() -> None:
    with pytest.raises(ValidationError, match="needs an `oauth` block"):
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "slack": slack_channel_block(mcp=True),
                }
            }
        )


def test_a_slack_oauth_client_stores_tokens_and_so_needs_the_key() -> None:
    deployment = {
        "tentacles": {
            "inkling": {"type": "inkling", "models": [{"name": "openai:gpt-4o"}]},
            "slack": slack_channel_block(
                mcp=True, oauth={"client_id": "1.2", "client_secret": "shh"}
            ),
        }
    }

    with pytest.raises(
        ValidationError,
        match=r"oauth\.encryption_key is required when tentacles\.slack\.oauth",
    ):
        OctomateConfig.model_validate(deployment)

    config = OctomateConfig.model_validate(
        {
            **deployment,
            "oauth": {"encryption_key": urlsafe_b64encode(bytes(range(32))).decode()},
        }
    )
    slack = config.tentacles["slack"]
    assert isinstance(slack, SlackChannelConfig)
    assert slack.oauth is not None
    # What the forwarded tools need and nothing that posts as the person.
    assert slack.oauth.scopes == SLACK_MCP_SCOPES
    assert "chat:write" not in slack.oauth.scopes
    assert str(slack.oauth.callback_base_uri) == "http://localhost:8000/"


def test_users_are_not_loaded_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path))
    (tmp_path / "users.yaml").write_text("users: [invalid old configuration]\n")

    config = OctomateConfig()

    assert "users.yaml" not in CONFIG_FILES
    assert "users" not in OctomateConfig.model_fields
    assert "users" not in config.model_dump()


@pytest.mark.parametrize("config_type", [ClaudeCodeConfig, CodexConfig, DeepseekConfig])
def test_harness_configs_discover_models(
    config_type: type[ClaudeCodeConfig | CodexConfig | DeepseekConfig],
) -> None:
    config = config_type()
    assert config.claims == {}


@pytest.mark.parametrize("config_type", [ClaudeCodeConfig, CodexConfig, DeepseekConfig])
@pytest.mark.parametrize("models", [None, ["provider:future-model"]])
def test_harness_configs_ignore_obsolete_model_lists(
    config_type: type[ClaudeCodeConfig | CodexConfig | DeepseekConfig],
    models: list[str] | None,
) -> None:
    config = config_type.model_validate({"models": models})
    assert "models" not in config.model_dump()
    assert not hasattr(config, "models")


def test_channel_rejects_obsolete_model_bindings() -> None:
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        ChannelConfig.model_validate(
            {"type": "fake", "agents": [{"agent": "claude", "model": "opus"}]}
        )


def test_runtime_model_names_are_open_and_can_delegate_the_default() -> None:
    assert (
        AgentModelConfig(agent="codex", model="openai:a-future-model").model
        == "openai:a-future-model"
    )
    assert AgentModelConfig(agent="claude", model=None).model is None


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_configured_oauth_requires_its_client_secret(method: str) -> None:
    payload = configured_mcp().model_dump(mode="json")
    payload["flows"][0]["token_endpoint_auth_method"] = method
    with pytest.raises(ValidationError, match="requires client_secret"):
        OAuthMcpConfig.model_validate(payload)
    payload["client_secret"] = "application-secret"
    config = OAuthMcpConfig.model_validate(payload)
    assert "application-secret" not in config.model_dump_json()


def test_configured_oauth_refuses_an_unused_client_secret() -> None:
    payload = configured_mcp().model_dump(mode="json")
    payload["client_secret"] = "application-secret"
    with pytest.raises(ValidationError, match="client_secret requires"):
        OAuthMcpConfig.model_validate(payload)


@pytest.mark.parametrize(
    "url", ["http://auth.example/token", "ftp://auth.example/token"]
)
def test_configured_oauth_requires_https_endpoints(url: str) -> None:
    payload = configured_mcp().model_dump(mode="json")
    payload["flows"][0]["token_endpoint"] = url
    with pytest.raises(ValidationError, match="https"):
        OAuthMcpConfig.model_validate(payload)


def test_authorization_code_mcp_requires_callback_configuration() -> None:
    payload = configured_mcp().model_dump(mode="json")
    payload["flows"] = [
        {
            "type": "authorization_code",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        }
    ]
    with pytest.raises(ValidationError, match=r"oauth\.callback_base_uri is required"):
        OctomateConfig.model_validate(
            {
                "tentacles": {"work": payload},
                "oauth": {"encryption_key": "x" * 43 + "="},
            }
        )


def test_startup_builds_tentacles_by_type_and_keeps_their_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            "tentacles": {
                "web": {"type": "trunkline", "agents": ["coding"]},
                "coding": {"type": "codex"},
                "tools": {"type": "bare", "url": "https://mcp.example/mcp"},
                "disabled": {
                    "type": "bare",
                    "url": "https://disabled.example/mcp",
                    "enabled": False,
                },
            }
        }
    )
    monkeypatch.setattr(application, "config", config)
    with patch("logfire.configure"), patch("logging.basicConfig"):
        host = application.create_app()
    assert list(host.tentacles) == ["web", "coding", "tools"]
    assert list(host.agents) == ["coding"]
    assert list(host.channels) == ["web"]
    assert list(host.mcp.tentacles) == ["tools"]
    assert host.channels["web"].agent_ids == ["coding"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_configured_oauth_rejects_empty_or_duplicate_flows(duplicate: bool) -> None:
    payload = configured_mcp().model_dump(mode="json")
    payload["flows"] = payload["flows"] * 2 if duplicate else []
    with pytest.raises(ValidationError):
        OAuthMcpConfig.model_validate(payload)
