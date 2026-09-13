from __future__ import annotations

import errno
import os
import sqlite3
import subprocess
import sys
from functools import partial
from io import StringIO
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastmcp import FastMCP
from octomate_cli import deployment
from octomate_cli.mcp import McpPreset
from octomate_protocol.deployment import DatabaseBackup
from pydantic import SecretStr, TypeAdapter
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from octomate.config import CONFIG_FILES, AuthConfig, OAuthMcpConfig, OctomateConfig
from octomate.config.agents import AgentConfig, CodexConfig, DeepseekConfig
from octomate.config.channels import ChannelConfig, TrunklineChannelConfig
from octomate.config.database import database_settings
from octomate.mcp.base import KnownBearers


@pytest.fixture
def preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OCTOMATE_HOME", str(tmp_path / "config"))
    url = f"sqlite+aiosqlite:///{tmp_path / 'octomate.db'}"
    monkeypatch.setenv("OCTOMATE_DB_URL", url)
    monkeypatch.setattr(database_settings, "db_url", url)
    monkeypatch.setitem(OctomateConfig.model_config, "env_file", ".env")
    return tmp_path


@pytest.mark.parametrize("console", [True, False])
def test_prepare_creates_valid_private_claude_configuration(
    preparation: Path, console: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    deployment.prepare(8123, ["trunkline"] if console else [], ["claude"])
    config = OctomateConfig()
    assert config.host == IPv4Address("127.0.0.1")
    assert config.port == 8123
    assert [
        name
        for name, entry in config.tentacles.items()
        if isinstance(entry, AgentConfig) and entry.enabled
    ] == ["claude"]
    assert list(config.tentacles) == (
        ["claude", "trunkline"] if console else ["claude"]
    )
    assert config.projects == {}
    assert all(value is None for value in config.providers.model_dump().values())
    assert config.auth is not None
    salts = {
        config.auth.access_token_salt.get_secret_value(),
        config.auth.refresh_token_salt.get_secret_value(),
        config.auth.api_key_salt.get_secret_value(),
    }
    assert len(salts) == 3
    assert all(len(salt) >= 32 for salt in salts)
    output = capsys.readouterr().out
    assert all(salt not in output for salt in salts)
    home = preparation / "config"
    assert {path.name for path in home.iterdir()} == set(CONFIG_FILES)
    auth_yaml = yaml.safe_load((home / "auth.yaml").read_text())["auth"]
    assert not any(name.endswith("_salt") for name in auth_yaml)
    assert "**********" not in (home / "auth.yaml").read_text()
    assert preparation.stat().st_mode & 0o777 == 0o700
    assert home.stat().st_mode & 0o777 == 0o700
    for path in [
        preparation / ".env",
        preparation / "CONFIGURATION.md",
        *home.iterdir(),
    ]:
        assert path.stat().st_mode & 0o777 == 0o600
    assert not (preparation / "octomate.db").exists()
    assert {path.name for path in preparation.iterdir()} == {
        "config",
        ".env",
        "CONFIGURATION.md",
    }


@pytest.mark.parametrize(
    "agents",
    [["codex"], ["claude", "codex"], ["deepseek"], ["claude", "codex", "deepseek"]],
)
def test_prepare_enables_selected_agents_and_routes_console(
    preparation: Path, agents: list[str]
) -> None:
    deployment.prepare(8123, ["trunkline"], agents)
    config = OctomateConfig()
    assert [
        name
        for name, entry in config.tentacles.items()
        if isinstance(entry, AgentConfig) and entry.enabled
    ] == agents
    assert isinstance(config.tentacles["trunkline"], TrunklineChannelConfig)
    assert config.tentacles["trunkline"].agents == agents
    if "deepseek" in agents:
        assert isinstance(config.tentacles["deepseek"], DeepseekConfig)
        assert config.tentacles["deepseek"].executable == "dsh"
        checklist = (preparation / "CONFIGURATION.md").read_text()
        assert "DSH (experimental)" in checklist
        assert "tentacles.deepseek.executable" in checklist
    assert not (preparation / "octomate.db").exists()


def test_prepare_scaffolds_selected_channels_without_collecting_credentials(
    preparation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OCTOMATE__TENTACLES__SLACK__BOT_TOKEN", "existing-secret-do-not-copy"
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "another-secret-do-not-copy")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maintenance",
            "prepare",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--agent",
            "codex",
            "--channel",
            "slack",
            "--channel",
            "lark",
            "--channel",
            "discord",
            "--channel",
            "trunkline",
        ],
    )
    deployment.main()
    config = OctomateConfig()
    assert set(config.tentacles) == {
        "claude",
        "codex",
        "slack",
        "lark",
        "discord",
        "trunkline",
    }
    for name, channel in config.tentacles.items():
        if not isinstance(channel, ChannelConfig):
            continue
        assert channel.agents == ["claude", "codex"]
        assert channel.enabled == (name == "trunkline")
        assert not channel.mcp
    home = preparation / "config"
    channel_yaml = yaml.safe_load((home / "tentacles.yaml").read_text())["tentacles"]
    assert channel_yaml["slack"]["app_id"] == "FILL_IN_SLACK_APP_ID"
    assert channel_yaml["slack"]["bot_token"] == "FILL_IN_SLACK_BOT_TOKEN"
    assert channel_yaml["slack"]["app_token"] == "FILL_IN_SLACK_APP_TOKEN"
    assert channel_yaml["lark"]["app_id"] == "FILL_IN_LARK_APP_ID"
    assert channel_yaml["lark"]["app_secret"] == "FILL_IN_LARK_APP_SECRET"
    assert channel_yaml["discord"]["bot_token"] == "FILL_IN_DISCORD_BOT_TOKEN"
    assert "**********" not in (home / "tentacles.yaml").read_text()
    dotenv = (preparation / ".env").read_text()
    assert len(dotenv.splitlines()) == 3
    assert all(line.startswith("OCTOMATE__AUTH__") for line in dotenv.splitlines())
    for path in [
        *home.iterdir(),
        preparation / ".env",
        preparation / "CONFIGURATION.md",
    ]:
        assert "existing-secret-do-not-copy" not in path.read_text()
        assert "another-secret-do-not-copy" not in path.read_text()
    assert not (preparation / "octomate.db").exists()


@pytest.mark.parametrize(
    "channels", [[], ["slack"], ["lark"], ["discord"], ["trunkline"]]
)
def test_checklist_and_templates_only_include_selected_components(
    preparation: Path, channels: list[str]
) -> None:
    deployment.prepare(8123, channels, ["codex"])
    home = preparation / "config"
    config = OctomateConfig()
    assert list(config.tentacles) == ["codex", *channels]
    agent_yaml = yaml.safe_load((home / "tentacles.yaml").read_text())["tentacles"]
    assert list(agent_yaml) == ["codex", *channels]
    checklist = (preparation / "CONFIGURATION.md").read_text()
    assert "tentacles.codex.runtime" in checklist
    assert "tentacles.claude" not in checklist
    for name in ("slack", "lark", "discord", "trunkline"):
        assert (f"tentacles.{name}." in checklist) == (name in channels)
    assert "template structure only" in checklist
    assert "schema check does not verify credentials" in checklist
    assert f"octomate service init --prepare --root {preparation}" in checklist
    assert "without source or selection flags" in checklist
    assert "connector tool call" in checklist
    assert "restart the GUI service" in checklist
    if channels and channels != ["trunkline"]:
        assert "YAML overrides `.env`" in checklist
        assert f"tentacles.{channels[0]}.enabled: true" in checklist
    assert (preparation / "CONFIGURATION.md").stat().st_mode & 0o777 == 0o600


def test_claude_checklist_preserves_native_login(preparation: Path) -> None:
    deployment.prepare(8123, [], ["claude"])
    checklist = (preparation / "CONFIGURATION.md").read_text()
    assert "Keychain credentials" in checklist
    assert "plugins and Claude.ai connectors" in checklist
    assert "do not replace that login with a setup token" in checklist
    assert "tentacles.codex" not in checklist


@pytest.mark.parametrize("channels", [["trunkline", "trunkline"], ["napcat"]])
def test_prepare_refuses_duplicate_or_unsupported_channels(
    preparation: Path, channels: list[str]
) -> None:
    with pytest.raises(ValueError, match="Choose"):
        deployment.prepare(8123, channels, ["claude"])
    assert list(preparation.iterdir()) == []


@pytest.mark.parametrize(
    "existing", [".env", "config/tentacles.yaml", "CONFIGURATION.md"]
)
def test_prepare_does_not_overwrite_existing_configuration(
    preparation: Path, existing: str
) -> None:
    path = preparation / existing
    path.parent.mkdir(exist_ok=True)
    path.write_text("keep this configuration")
    with pytest.raises(ValueError, match="refusing"):
        deployment.prepare(8000, [], ["claude"])
    assert path.read_text() == "keep this configuration"
    assert not (preparation / "octomate.db").exists()


def test_prepare_validates_before_publishing(preparation: Path) -> None:
    with patch(
        "octomate_cli.deployment.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "check"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            deployment.prepare(8000, [], ["claude"])
    assert list(preparation.iterdir()) == []


@pytest.mark.parametrize("variable", ["OCTOMATE_HOME", "OCTOMATE_DB_URL"])
def test_prepare_requires_explicit_installation_paths(
    preparation: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(ValueError, match="OCTOMATE"):
        deployment.prepare(8000, [], ["claude"])
    assert list(preparation.iterdir()) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["prepare"],
        ["prepare", "--port", "0"],
        ["prepare", "--port", "65536"],
        ["check", "--port", "8000"],
        ["check", "--console"],
        ["check", "--channel", "slack"],
        ["prepare", "--port", "8123", "--agent", "claude", "--channel", "napcat"],
    ],
)
def test_prepare_options_are_scoped_and_validated(
    preparation: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["maintenance", *arguments])
    with pytest.raises(SystemExit) as failure:
        deployment.main()
    assert failure.value.code == 2
    assert list(preparation.iterdir()) == []


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "production.sqlite3"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("OCTOMATE_DB_URL", url)
    monkeypatch.setattr(database_settings, "db_url", url)
    return path


def test_backup_includes_wal_and_preserves_source(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE work (content TEXT)")
        connection.execute("INSERT INTO work VALUES ('irreplaceable')")
        connection.commit()
        snapshot = deployment.backup_database(database)
        assert snapshot.backup is not None
        with sqlite3.connect(snapshot.backup) as backup:
            assert backup.execute("SELECT content FROM work").fetchall() == [
                ("irreplaceable",)
            ]
        assert connection.execute("SELECT content FROM work").fetchall() == [
            ("irreplaceable",)
        ]


def test_missing_database_has_no_backup(database: Path) -> None:
    snapshot = deployment.backup_database(database)
    assert snapshot.backup is None
    assert not database.exists()


def test_backup_uses_the_explicit_service_root(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "service"
    monkeypatch.setenv("OCTOMATE_DEPLOYMENT_ROOT", str(root))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE work (content TEXT)")
    snapshot = deployment.backup_database(database)
    assert snapshot.backup is not None
    assert snapshot.backup.parent == root / "backups"


@pytest.mark.parametrize("current", [True, False])
def test_ready_checks_revision_without_writing_database(
    database: Path, monkeypatch: pytest.MonkeyPatch, current: bool
) -> None:
    head = ScriptDirectory.from_config(
        Config(str(deployment.ALEMBIC_INI))
    ).get_current_head()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute(
            "INSERT INTO alembic_version VALUES (?)", (head if current else "old",)
        )
    before = database.read_bytes()
    monkeypatch.setattr(sys, "argv", ["maintenance", "ready"])
    if current:
        deployment.main()
    else:
        with pytest.raises(ValueError, match="schema is not current"):
            deployment.main()
    assert database.read_bytes() == before


def test_ready_does_not_initialize_missing_database(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["maintenance", "ready"])
    with pytest.raises(ValueError, match="schema is not current"):
        deployment.main()
    assert not database.exists()


@pytest.mark.parametrize("stops", [True, False])
@pytest.mark.parametrize("action", ["backup", "stopped"])
def test_backup_waits_for_server_shutdown(
    database: Path, monkeypatch: pytest.MonkeyPatch, stops: bool, action: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["maintenance", action])
    monkeypatch.setattr(deployment, "OctomateConfig", OctomateConfig)
    monkeypatch.setattr(deployment.time, "sleep", lambda duration: None)
    busy = OSError(errno.EADDRINUSE, "Server is still listening")
    with (
        patch("octomate_cli.deployment.socket.socket") as sockets,
        patch(
            "octomate_cli.deployment.time.monotonic",
            side_effect=[0, 0 if stops else 31],
        ),
    ):
        listener = sockets.return_value.__enter__.return_value
        listener.bind.side_effect = [busy, None] if stops else [busy]
        if stops:
            deployment.main()
            assert listener.bind.call_count == 2
        else:
            with pytest.raises(TimeoutError, match="did not stop"):
                deployment.main()
    assert not database.exists()


def test_migration_rejects_changed_target(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = DatabaseBackup(database=database, backup=None)
    url = f"sqlite+aiosqlite:///{database.parent / 'other.db'}"
    monkeypatch.setenv("OCTOMATE_DB_URL", url)
    monkeypatch.setattr(database_settings, "db_url", url)
    with pytest.raises(ValueError, match="target changed"):
        deployment.migrate(snapshot)
    assert not database.exists()


def test_nonempty_unversioned_database_is_not_initialized(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE work (content TEXT)")
    snapshot = deployment.backup_database(database)
    with pytest.raises(ValueError, match="no Alembic revision"):
        deployment.migrate(snapshot)


def test_actual_migrations_initialize_empty_database(database: Path) -> None:
    deployment.migrate(DatabaseBackup(database=database, backup=None))
    head = ScriptDirectory.from_config(
        Config(str(deployment.ALEMBIC_INI))
    ).get_current_head()
    assert deployment.revisions(database) == (head,)
    with sqlite3.connect(database) as connection:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'thread_messages_fts'"
        ).fetchone()
    assert definition is not None
    assert "porter unicode61" in definition[0]
    before = database.read_bytes()
    deployment.migrate(deployment.backup_database(database))
    assert database.read_bytes() == before


def test_failed_rehearsal_never_migrates_source(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('old')")
    snapshot = deployment.backup_database(database)
    calls: list[str] = []

    def fail(arguments: list[str], *, env: dict[str, str], check: bool) -> None:
        assert check
        assert arguments[-2:] == ["upgrade", "head"]
        assert env["OCTOMATE_DB_URL"] != os.environ["OCTOMATE_DB_URL"]
        calls.append(env["OCTOMATE_DB_URL"])
        raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        deployment.migrate(snapshot)
    assert len(calls) == 1
    assert deployment.revisions(database) == ("old",)


@pytest.mark.parametrize("previous", ["88a4f648c14d", "f973cff9f077"])
def test_actual_upgrade_rehearses_on_a_copy(database: Path, previous: str) -> None:
    script = ScriptDirectory.from_config(Config(str(deployment.ALEMBIC_INI)))
    head = script.get_current_head()
    assert head is not None
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(deployment.ALEMBIC_INI),
            "upgrade",
            previous,
        ],
        check=True,
    )
    snapshot = deployment.backup_database(database)
    assert snapshot.backup is not None
    deployment.migrate(snapshot)
    assert deployment.revisions(database) == (head,)
    assert deployment.revisions(snapshot.backup) == (previous,)


def test_actual_upgrade_backfills_search_and_downgrade_keeps_source(
    database: Path,
) -> None:
    script = ScriptDirectory.from_config(Config(str(deployment.ALEMBIC_INI)))
    head = script.get_current_head()
    assert head is not None
    revision = script.get_revision("88a4f648c14d")
    assert revision is not None
    previous = revision.down_revision
    assert isinstance(previous, str)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(deployment.ALEMBIC_INI),
            "upgrade",
            previous,
        ],
        check=True,
    )
    message_id = "00000000000070008000000000000001"
    thread_id = "00000000000070008000000000000002"
    sender_id = "00000000000070008000000000000003"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO user_profiles(
                id, channel_tentacle_id, channel_user_id, name
            ) VALUES (?, 'test', 'alice', 'Alice')
            """,
            (sender_id,),
        )
        connection.execute(
            """
            INSERT INTO threads(
                id,
                channel_tentacle_id,
                chat_type,
                chat_id,
                status,
                created_at,
                updated_at,
                kind
            ) VALUES (?, 'test', 'dm', 'alice', 'active', ?, ?, 'dm')
            """,
            (thread_id, "2026-09-07 00:00:00", "2026-09-07 00:00:00"),
        )
        connection.execute(
            """
            INSERT INTO thread_messages(
                id,
                thread_id,
                reply_id,
                happened_at,
                direction,
                actor_kind,
                user_id,
                segments,
                message_text,
                raw,
                created_at,
                sender_id
            ) VALUES (?, ?, '', ?, 'inbound', 'human', 'alice', '[]', ?, '', ?, ?)
            """,
            (
                message_id,
                thread_id,
                "2026-09-07 00:00:00",
                "migration backfill marker",
                "2026-09-07 00:00:00",
                sender_id,
            ),
        )
        connection.commit()
    snapshot = deployment.backup_database(database)
    assert snapshot.backup is not None
    deployment.migrate(snapshot)
    assert deployment.revisions(database) == (head,)
    assert deployment.revisions(snapshot.backup) == (previous,)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT message_id
            FROM thread_messages_fts
            WHERE thread_messages_fts MATCH 'migration'
            """
        ).fetchall() == [(message_id,)]

    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(deployment.ALEMBIC_INI),
            "downgrade",
            previous,
        ],
        check=True,
    )

    assert deployment.revisions(database) == (previous,)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT message_text FROM thread_messages WHERE id = ?", (message_id,)
        ).fetchone() == ("migration backfill marker",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'thread_messages_fts'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("auth_configured", [False, True])
@pytest.mark.parametrize("console_enabled", [False, True])
@pytest.mark.parametrize("console_registered", [False, True])
@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1"])
async def test_verification_checks_protected_mcp_and_console_routes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    auth_configured: bool,
    console_enabled: bool,
    console_registered: bool,
    host: str,
) -> None:
    config = OctomateConfig()
    config.host = IPv4Address(host)
    config.tentacles["codex"] = CodexConfig()
    config.tentacles["trunkline"] = TrunklineChannelConfig(
        enabled=console_enabled,
        agents=["codex"],
    )
    if auth_configured:
        config.auth = AuthConfig(
            access_token_salt=SecretStr("access-test-salt-123"),
            refresh_token_salt=SecretStr("refresh-test-salt-123"),
            api_key_salt=SecretStr("api-key-test-salt-123"),
        )
    server = FastMCP("octomate", auth=KnownBearers())

    @server.tool
    def hello() -> str:
        return "hello"

    api = server.http_app(path="/octomate/mcp", stateless_http=True)

    async def console(request: Request) -> Response:
        assert request.url.hostname == host
        return Response("console", status_code=401 if auth_configured else 503)

    if console_registered:
        api.routes.append(Route("/api/trunkline/health", console))
        api.routes.append(Route("/api/trunkline/threads", console))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.ASGITransport(app=api)),
    )
    async with api.lifespan(api):
        if console_enabled != console_registered:
            with pytest.raises(ValueError, match="console route"):
                await deployment.verify(config)
        else:
            await deployment.verify(config)
            output = capsys.readouterr().out
            assert "protected local Octomate MCP" in output


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "192.0.2.1"])
def test_maintenance_requires_an_explicit_bind_address(
    database: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    config = OctomateConfig()
    config.host = IPv4Address(host)
    config.tentacles["codex"] = CodexConfig()
    config.tentacles["trunkline"] = TrunklineChannelConfig(agents=["codex"])
    monkeypatch.setattr(sys, "argv", ["maintenance", "check"])
    monkeypatch.setattr(deployment, "OctomateConfig", lambda: config)
    if host == "0.0.0.0":
        with pytest.raises(ValueError, match="explicit IPv4 bind address"):
            deployment.main()
    else:
        deployment.main()
    assert not database.exists()


def test_prepare_reads_mcp_presets_and_saves_private_oauth_configuration(
    preparation: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcps = [
        McpPreset(provider="github", name="github_work", client_id="test-work"),
        McpPreset(
            provider="github",
            name="github_personal",
            client_id="test-personal",
            read_only=True,
        ),
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maintenance",
            "prepare",
            "--port",
            "8123",
            "--agent",
            "codex",
            "--mcp-presets",
        ],
    )
    monkeypatch.setattr(
        sys, "stdin", StringIO(TypeAdapter(list[McpPreset]).dump_json(mcps).decode())
    )
    deployment.main()
    config = OctomateConfig()
    assert set(config.tentacles) == {"codex", "github_work", "github_personal"}
    dotenv = (preparation / ".env").read_text()
    tentacles = yaml.safe_load((preparation / "config/tentacles.yaml").read_text())[
        "tentacles"
    ]
    assert list(tentacles) == ["codex", "github_work", "github_personal"]
    for preset in mcps:
        assert config.tentacles[preset.name] == OAuthMcpConfig.model_validate(
            preset.configuration() | {"client_secret": "FILL_IN_OAUTH_CLIENT_SECRET"}
        )
        assert "client_secret" not in tentacles[preset.name]
        assert (
            f"OCTOMATE__TENTACLES__{preset.name.upper()}__CLIENT_SECRET=FILL_IN_OAUTH_CLIENT_SECRET"
            in dotenv
        )
    assert str(config.oauth.callback_base_uri) == "http://localhost:8123/"
    assert config.oauth.encryption_key is not None
    key = config.oauth.encryption_key.get_secret_value()
    assert len(key) == 43
    assert (
        f"OCTOMATE__OAUTH__ENCRYPTION_KEY={key}" in (preparation / ".env").read_text()
    )
    for path in (preparation / "config").iterdir():
        assert key not in path.read_text()
        assert "**********" not in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert key not in output
    assert "http://localhost:8123/oauth/github_work/callback" in output
    assert "http://localhost:8123/oauth/github_personal/callback" in output
    assert (preparation / ".env").stat().st_mode & 0o777 == 0o600
    assert not (preparation / "octomate.db").exists()


def test_prepare_rejects_duplicate_mcp_names_without_writing(preparation: Path) -> None:
    preset = McpPreset(provider="github", name="github", client_id="test-app")
    with pytest.raises(ValueError, match="MCP tentacle names must be unique"):
        deployment.prepare(8123, [], ["codex"], [preset, preset])
    assert list(preparation.iterdir()) == []


@pytest.mark.parametrize("name", ["codex", "trunkline"])
def test_prepare_rejects_mcp_names_used_by_other_tentacles(
    preparation: Path, name: str
) -> None:
    preset = McpPreset(provider="github", name=name, client_id="test-app")
    with pytest.raises(ValueError, match="Tentacle names must be unique"):
        deployment.prepare(8123, ["trunkline"], ["codex"], [preset])
    assert list(preparation.iterdir()) == []
