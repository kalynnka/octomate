from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from datetime import timedelta
from pathlib import Path
from typing import ClassVar, get_args

import pytest
from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import ValidationError
from pydantic_ai.settings import ThinkingEffort

from octomate.config import (
    AgentModelConfig,
    AgentsConfig,
    BareMcpConfig,
    ChannelConfig,
    ClaudeCodeConfig,
    ClaudeSSHConfig,
    CodexConfig,
    DeepseekConfig,
    DiscordChannelConfig,
    DiscordStreamConfig,
    GitHubMcpConfig,
    InklingConfig,
    LarkChannelConfig,
    ModelConfig,
    NapcatChannelConfig,
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
    assert live.channels == {}


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
    assert config.agents.configured_agents == []
    assert config.channels == {}
    assert config.providers.deepseek is None
    assert config.auth is None


def test_configured_agents_returns_enabled_config_objects() -> None:
    claude = ClaudeCodeConfig(enabled=False)
    codex = CodexConfig()
    agents = AgentsConfig(claude=claude, codex=codex)

    configured_agents = agents.configured_agents
    [enabled] = configured_agents
    assert enabled is codex
    assert enabled.id == "codex"

    claude.enabled = True
    codex.enabled = False
    assert agents.configured_agents is configured_agents
    assert agents.configured_agents == [codex]


def test_configured_agents_preserves_model_iteration_and_serialization() -> None:
    agents = AgentsConfig(
        inkling=InklingConfig(models=[ModelConfig(name="openai:gpt-4o")]),
        claude=ClaudeCodeConfig(enabled=False),
        codex=CodexConfig(),
        deepseek=DeepseekConfig(),
    )
    fields = dict(agents)
    assert set(fields) == {"inkling", "claude", "codex", "deepseek"}
    assert fields["claude"] is agents.claude
    config = OctomateConfig(agents=agents)
    serialized = config.model_dump(mode="json")
    assert set(serialized["agents"]) == set(fields)
    assert serialized["agents"]["claude"]["enabled"] is False
    assert all("id" not in agent for agent in serialized["agents"].values())

    restored = OctomateConfig.model_validate_json(config.model_dump_json())
    assert restored.agents == config.agents
    assert [agent.id for agent in restored.agents.configured_agents] == [
        "inkling",
        "codex",
        "deepseek",
    ]


@pytest.mark.parametrize("enabled", [True, False])
def test_agents_reject_duplicate_ids_across_harnesses(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(CodexConfig, "id", "claude")
    with pytest.raises(ValidationError, match="duplicate agent id 'claude'"):
        OctomateConfig.model_validate(
            {"agents": {"claude": {}, "codex": {"enabled": enabled}}}
        )


@pytest.mark.parametrize("second_id", ["claude", "another-subscription"])
@pytest.mark.parametrize("enabled", [True, False])
def test_additional_agent_configs_share_the_id_validation(
    second_id: str, enabled: bool
) -> None:
    class SubscriptionConfig(ClaudeCodeConfig):
        id: ClassVar[str] = second_id

    class SubscriptionAgentsConfig(AgentsConfig):
        subscription: ClaudeCodeConfig | None = None

    subscription = SubscriptionConfig(enabled=enabled)
    if second_id == "claude":
        with pytest.raises(ValidationError, match="duplicate agent id 'claude'"):
            SubscriptionAgentsConfig(
                claude=ClaudeCodeConfig(), subscription=subscription
            )
    else:
        agents = SubscriptionAgentsConfig(
            claude=ClaudeCodeConfig(), subscription=subscription
        )
        assert [agent.id for agent in agents.configured_agents] == (
            ["claude", second_id] if enabled else ["claude"]
        )


def test_channel_config_parses_supported_channels() -> None:
    config = OctomateConfig.model_validate(
        {
            "agents": {
                "inkling": {"models": [{"name": "openai:gpt-4o"}]},
            },
            "channels": {
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
            },
        }
    )

    assert isinstance(config.channels["slack"], SlackChannelConfig)
    assert isinstance(config.channels["lark"], LarkChannelConfig)
    assert isinstance(config.channels["napcat"], NapcatChannelConfig)
    assert isinstance(config.channels["discord"], DiscordChannelConfig)
    assert isinstance(config.channels["discord"].stream, DiscordStreamConfig)
    assert config.channels["slack"].stream.flush_interval == 0.2
    assert config.channels["slack"].stream.min_chars == 20
    assert config.channels["lark"].stream.flush_interval == 0.2
    assert config.channels["lark"].stream.min_chars == 20
    assert config.channels["napcat"].stream.enabled is False
    assert config.channels["discord"].stream.enabled is False
    assert config.channels["discord"].stream.flush_interval == 0.2


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
            "agents": {
                "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                "claude": {},
                "codex": {},
            },
            "channels": {
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
            },
        }
    )

    assert config.channels["slack"] is not None
    assert config.channels["slack"].agents == [
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
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["ghost"],
                    }
                },
            }
        )

    [error] = exc_info.value.errors()
    assert error["type"] == "channel_agent_route"
    assert error["loc"] == ("channels", "slack", "agents", 0)
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_agent_route_validation_reports_all_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {},
                },
                "channels": {
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
        ): "'ghost' does not match a configured agent tentacle",
        (
            "channels",
            "lark",
            "agents",
            0,
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
                        "agents": ["ghost"],
                    }
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0)
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
                        "agents": ["claude"],
                    },
                },
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0)
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_claude_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "claude": {
                        "enabled": False,
                    },
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["claude"],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0)
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_codex_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "codex": {
                        "enabled": False,
                    },
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["codex"],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0)
    assert error["msg"] == "'codex' does not match a configured agent tentacle"


def test_channel_deepseek_route_requires_enabled_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig.model_validate(
            {
                "agents": {
                    "inkling": {"models": [{"name": "openai:gpt-4o"}]},
                    "deepseek": {
                        "enabled": False,
                    },
                },
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "xoxb-test",
                        "app_token": "xapp-test",
                        "agents": ["deepseek"],
                    },
                },
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "agents", 0)
    assert error["msg"] == "'deepseek' does not match a configured agent tentacle"


def test_a_scope_github_does_not_define_is_refused() -> None:
    # GitHub ignores a scope it does not recognise and returns a token quietly
    # missing that access, so a typo has to fail here instead.
    config = OctomateConfig.model_validate(
        {
            "mcp": {
                "github": {
                    "type": "github",
                    "client_id": "Iv1.test",
                    "scopes": ["repo", "workflow"],
                }
            },
            "oauth": {"encryption_key": "x" * 43 + "="},
        }
    )
    github = config.mcp["github"]
    assert isinstance(github, GitHubMcpConfig)
    assert github.scopes == ["repo", "workflow"]

    # Validated, not constructed: a scope arrives as untyped YAML.
    with pytest.raises(ValidationError, match="Input should be"):
        GitHubMcpConfig.model_validate(
            {"client_id": "Iv1.test", "scopes": ["workfl0w"]}
        )


def test_config_parses_each_mcp_tentacle_type() -> None:
    config = OctomateConfig.model_validate(
        {
            "mcp": {
                "github": {
                    "type": "github",
                    "client_id": "Iv1.test",
                    "scopes": ["repo", "read:org"],
                    "read_only": True,
                },
                "linear": {
                    "type": "bare",
                    "url": "https://mcp.linear.app/mcp",
                    "token": "lin_test",
                },
            },
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )

    github = config.mcp["github"]
    assert isinstance(github, GitHubMcpConfig)
    assert github.enabled is True
    assert github.read_only is True
    assert github.client_id == "Iv1.test"
    assert github.scopes == ["repo", "read:org"]
    # A partial block still keeps the GitHub endpoint default.
    assert github.url == "https://api.githubcopilot.com/mcp/"

    linear = config.mcp["linear"]
    assert isinstance(linear, BareMcpConfig)
    assert linear.prefix is None
    assert linear.enabled is True
    assert linear.url == "https://mcp.linear.app/mcp"


def test_a_linked_account_needs_the_encryption_key() -> None:
    # The tokens it stores are what the key protects; a bare server stores none.
    OctomateConfig.model_validate(
        {
            "mcp": {
                "notion": {
                    "type": "bare",
                    "url": "https://mcp.notion.com/mcp",
                    "token": "ntn_x",
                }
            }
        }
    )
    with pytest.raises(
        ValidationError, match=r"oauth\.encryption_key is required when mcp\.github"
    ):
        OctomateConfig.model_validate(
            {"mcp": {"github": {"type": "github", "client_id": "Iv1.test"}}}
        )


def test_mcp_server_token_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Structure in YAML, secret in the environment: the key names the env var.
    monkeypatch.setenv("OCTOMATE__MCP__LINEAR__TOKEN", "lin_from_env")

    config = OctomateConfig.model_validate(
        {"mcp": {"linear": {"type": "bare", "url": "https://mcp.linear.app/mcp"}}}
    )

    linear = config.mcp["linear"]
    assert isinstance(linear, BareMcpConfig)
    assert linear.token.get_secret_value() == "lin_from_env"


def test_github_oauth_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # `type` too: it is the discriminator, so without it the block resolves to no
    # tentacle at all — the local octomate.yaml used to supply it by accident.
    monkeypatch.setenv("OCTOMATE__MCP__GITHUB__TYPE", "github")
    monkeypatch.setenv("OCTOMATE__MCP__GITHUB__CLIENT_ID", "Iv1.env")
    monkeypatch.setenv(
        "OCTOMATE__OAUTH__ENCRYPTION_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )

    config = OctomateConfig()

    github = config.mcp["github"]
    assert isinstance(github, GitHubMcpConfig)
    assert github.client_id == "Iv1.env"
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
    (tmp_path / "mcp.yaml").write_text("mcp_pool:\n  idle_timeout: 1200\n")
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
            "mcp": {
                "github_work": {"type": "github", "client_id": "Iv1.a"},
                "github_home": {
                    "type": "github",
                    "client_id": "Iv1.b",
                },
            },
            "oauth": {"encryption_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
        }
    )

    work = config.mcp["github_work"]
    home = config.mcp["github_home"]
    assert isinstance(work, GitHubMcpConfig)
    assert isinstance(home, GitHubMcpConfig)
    assert (work.client_id, home.client_id) == ("Iv1.a", "Iv1.b")


def test_an_mcp_block_without_a_type_is_refused() -> None:
    # Nothing else in the block says which tentacle builds it.
    with pytest.raises(ValidationError, match="tag"):
        OctomateConfig.model_validate({"mcp": {"linear_home": {"client_id": "lin_b"}}})


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
