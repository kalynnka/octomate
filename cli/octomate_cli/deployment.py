"""Internal maintenance entry point; run only in the installed service environment."""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from importlib.resources import files
from ipaddress import IPv4Address
from pathlib import Path

import httpx
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from octomate_protocol.deployment import DatabaseBackup
from pydantic import SecretStr, TypeAdapter
from sqlalchemy.engine import make_url

from octomate.config import (
    AgentsConfig,
    AuthConfig,
    LogfireConfig,
    LoggingConfig,
    MirrorsConfig,
    OAuthConfig,
    OctomateConfig,
    ProvidersConfig,
    WorkspacesConfig,
)
from octomate.config.channels import (
    ChannelConfigVariant,
    DiscordChannelConfig,
    LarkChannelConfig,
    SlackChannelConfig,
    TrunklineChannelConfig,
)
from octomate.config.database import database_settings
from octomate.config.mcp import McpConfigVariant
from octomate.mcp.server import OCTOMATE_MCP_PATH
from octomate_cli.mcp import McpPreset

ALEMBIC_INI = Path(str(files("octomate").joinpath("migrations", "alembic.ini")))

CHANNEL_PLACEHOLDERS: dict[str, dict[str, str]] = {
    "slack": {
        "app_id": "FILL_IN_SLACK_APP_ID",
        "bot_token": "FILL_IN_SLACK_BOT_TOKEN",
        "app_token": "FILL_IN_SLACK_APP_TOKEN",
    },
    "lark": {
        "app_id": "FILL_IN_LARK_APP_ID",
        "app_secret": "FILL_IN_LARK_APP_SECRET",
    },
    "discord": {"bot_token": "FILL_IN_DISCORD_BOT_TOKEN"},
    "trunkline": {},
}


def configuration_checklist(agents: list[str], channels: list[str], root: Path) -> str:
    instructions = [
        "# Complete this service configuration\n",
        "This scaffold validates template structure only. Setup remains incomplete; "
        "no database, account or live service has been created.\n",
        "## Selected agents\n",
    ]
    if "claude" in agents:
        instructions.append(
            "- [ ] Review `config/agents.yaml`: `agents.claude`, including `permission_mode`. "
            "Use the desktop account's native Claude login and Keychain credentials. "
            "Keep its existing settings, plugins and Claude.ai connectors; do not replace "
            "that login with a setup token. Verify these through Octomate after activation.\n"
        )
    if "codex" in agents:
        instructions.append(
            "- [ ] Review `config/agents.yaml`: `agents.codex.runtime`, "
            "`agents.codex.permission_mode` and `agents.codex.sandbox`. Confirm the desktop "
            "account's native Codex login and settings, then verify an actual request "
            "through Octomate after activation.\n"
        )
    if "deepseek" in agents:
        instructions.append(
            "- [ ] DSH (experimental): review `config/agents.yaml`: "
            "`agents.deepseek.executable`, `host`, `port`, `dsh_home` and "
            "`permission_mode`. Configure the harness's provider credentials yourself. "
            "Octomate attaches to an existing local harness or starts `dsh web` using "
            "these settings. Verify an actual request through Octomate after activation.\n"
        )
    instructions.append("\n## Selected channels\n")
    for channel in channels:
        if channel == "trunkline":
            instructions.append(
                "- [ ] Trunkline API is enabled in `config/channels.yaml`. The frontend "
                "build is separate: set `channels.trunkline.static_dir` to its existing "
                "build directory to serve it. Create an account after database setup "
                "and verify console sign-in.\n"
            )
        else:
            fields = ", ".join(
                f"`channels.{channel}.{field}`"
                for field in CHANNEL_PLACEHOLDERS[channel]
            )
            instructions.append(
                f"- [ ] In `config/channels.yaml`, replace the FILL_IN values at {fields}. "
                f"Then set `channels.{channel}.enabled: true` and verify a real channel "
                "conversation after activation.\n"
            )
        instructions.append(
            f"- [ ] Review `config/channels.yaml`: `channels.{channel}.agents` "
            "lists every selected agent; adjust the routing if needed.\n"
        )
    if not channels:
        instructions.append(
            "No channels selected; `config/channels.yaml` contains an empty mapping.\n"
        )
    if any(channel != "trunkline" for channel in channels):
        instructions.append(
            "If storing channel credentials in `.env` instead, use uppercase names "
            "`OCTOMATE__CHANNELS__<CHANNEL>__<FIELD>` and remove the corresponding YAML "
            "fields: YAML overrides `.env`, including FILL_IN placeholders.\n"
        )
    instructions.extend(
        [
            "\n## Before activation\n",
            "- [ ] Keep `.env` private. It contains generated Octomate auth salts; "
            "preparation does not collect channel credentials.\n",
            "- [ ] Recheck the completed configuration with "
            f"`octomate service init --prepare --root {shlex.quote(str(root))}` "
            "without source or selection flags. A successful schema check does not verify "
            "credentials or live readiness.\n",
            "- [ ] Review `control/io.octomate.server.plist` before installing it as a GUI "
            "LaunchAgent. Database initialization, the first account and service activation "
            "are separate deployment steps. The desktop account must be logged in; "
            "logging out stops the service, and reboot requires another desktop login.\n",
            "- [ ] After activation, send an actual agent request through Octomate, verify "
            "a connector tool call and the required plugins, then restart the GUI service "
            "and repeat verification. These checks have not run during preparation.\n",
        ]
    )
    return "\n".join(instructions)


def prepare(
    port: int,
    channels: list[str],
    agents: list[str],
    mcps: list[McpPreset] | None = None,
) -> None:
    if not agents or any(
        name not in {"claude", "codex", "deepseek"} for name in agents
    ):
        raise ValueError(
            "Choose at least one supported agent: claude, codex, deepseek (DSH; experimental)."
        )
    configured_agents = AgentsConfig.model_validate({name: {} for name in agents})
    root = Path.cwd()
    home = root / "config"
    if os.environ.get("OCTOMATE_HOME") != str(home):
        raise ValueError("Preparation requires OCTOMATE_HOME=<service root>/config.")
    if database_path() != root / "octomate.db":
        raise ValueError(
            "Preparation requires OCTOMATE_DB_URL=<service root>/octomate.db."
        )
    if (root / ".env").exists() or (root / ".env").is_symlink():
        raise ValueError("An .env already exists; refusing to replace service secrets.")
    if (root / "CONFIGURATION.md").exists() or (root / "CONFIGURATION.md").is_symlink():
        raise ValueError("CONFIGURATION.md already exists; refusing to overwrite it.")
    if home.is_symlink() or (
        home.exists() and (not home.is_dir() or any(home.iterdir()))
    ):
        raise ValueError(
            "The config directory must be empty; refusing to overwrite it."
        )

    salts = {
        name: secrets.token_urlsafe(32)
        for name in ("access_token_salt", "refresh_token_salt", "api_key_salt")
    }
    if mcps is None:
        mcps = []
    if len({mcp.name for mcp in mcps}) != len(mcps):
        raise ValueError("MCP tentacle names must be unique.")
    configured_mcps = TypeAdapter(dict[str, McpConfigVariant]).validate_python(
        {mcp.name: mcp.configuration() for mcp in mcps}
    )
    encryption_key = secrets.token_urlsafe(32) if configured_mcps else None
    routes = list(dict.fromkeys(agents))
    configured_channels: dict[str, ChannelConfigVariant] = {}
    for channel in channels:
        if channel not in CHANNEL_PLACEHOLDERS:
            raise ValueError(
                "Choose supported channels: slack, lark, discord, trunkline."
            )
        if channel in configured_channels:
            raise ValueError(f"Choose the {channel} channel only once.")
        fields = CHANNEL_PLACEHOLDERS[channel]
        if channel == "slack":
            configured_channels[channel] = SlackChannelConfig(
                app_id=fields["app_id"],
                bot_token=SecretStr(fields["bot_token"]),
                app_token=SecretStr(fields["app_token"]),
                agents=routes,
                enabled=False,
            )
        elif channel == "lark":
            configured_channels[channel] = LarkChannelConfig(
                app_id=fields["app_id"],
                app_secret=SecretStr(fields["app_secret"]),
                agents=routes,
                enabled=False,
            )
        elif channel == "discord":
            configured_channels[channel] = DiscordChannelConfig(
                bot_token=SecretStr(fields["bot_token"]), agents=routes, enabled=False
            )
        else:
            configured_channels[channel] = TrunklineChannelConfig(agents=routes)
    config = OctomateConfig(
        host=IPv4Address("127.0.0.1"),
        port=port,
        agents=configured_agents,
        auth=AuthConfig(
            access_token_salt=SecretStr(salts["access_token_salt"]),
            refresh_token_salt=SecretStr(salts["refresh_token_salt"]),
            api_key_salt=SecretStr(salts["api_key_salt"]),
        ),
        channels=configured_channels,
        projects={},
        providers=ProvidersConfig(),
        mcp=configured_mcps,
        logging=LoggingConfig(),
        logfire=LogfireConfig(),
        oauth=OAuthConfig(
            encryption_key=SecretStr(encryption_key) if encryption_key else None
        ),
        mirrors=MirrorsConfig(),
        workspaces=WorkspacesConfig(),
    )
    payload = config.model_dump(
        mode="json",
        exclude={
            "auth": set(salts),
            "agents": set(AgentsConfig.model_fields) - set(agents),
            "oauth": {"encryption_key"},
        },
    )
    for channel in channels:
        payload["channels"][channel].update(CHANNEL_PLACEHOLDERS[channel])
    sections = {
        "octomate": ("host", "port", "mirrors", "workspaces"),
        "agents": ("agents",),
        "channels": ("channels",),
        "auth": ("auth",),
        "projects": ("projects",),
        "providers": ("providers",),
        "mcp": ("mcp",),
        "observability": ("logging", "logfire"),
        "oauth": ("oauth",),
    }
    root.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as directory:
        staging = Path(directory)
        staged_home = staging / "config"
        staged_home.mkdir(mode=0o700)
        for name, fields in sections.items():
            path = staged_home / f"{name}.yaml"
            path.write_text(
                yaml.safe_dump(
                    {field: payload[field] for field in fields}, sort_keys=False
                )
            )
            path.chmod(0o600)
        dotenv = staging / ".env"
        dotenv.write_text(
            "".join(
                f"OCTOMATE__AUTH__{name.upper()}={salt}\n"
                for name, salt in salts.items()
            )
            + (
                f"OCTOMATE__OAUTH__ENCRYPTION_KEY={encryption_key}\n"
                if encryption_key
                else ""
            )
        )
        dotenv.chmod(0o600)
        checklist = staging / "CONFIGURATION.md"
        checklist.write_text(configuration_checklist(agents, channels, root))
        checklist.chmod(0o600)
        subprocess.run(
            [sys.executable, "-m", "octomate_cli.deployment", "check"],
            cwd=staging,
            env={**os.environ, "OCTOMATE_HOME": str(staged_home)},
            check=True,
        )
        home.mkdir(mode=0o700, exist_ok=True)
        home.chmod(0o700)
        for path in staged_home.iterdir():
            os.link(path, home / path.name)
        os.link(dotenv, root / ".env")
        os.link(checklist, root / checklist.name)
    print(
        "Prepared configuration templates; complete CONFIGURATION.md before activation. No database was created."
    )


def database_path() -> Path:
    explicit = os.environ.get("OCTOMATE_DB_URL")
    if not explicit or database_settings.db_url != explicit:
        raise ValueError("Set OCTOMATE_DB_URL explicitly to the service's database.")
    url = make_url(explicit)
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.query:
        raise ValueError("Server maintenance requires a file-backed SQLite database.")
    database = Path(url.database)
    if not database.is_absolute():
        raise ValueError("OCTOMATE_DB_URL must name an absolute SQLite path.")
    return database.resolve()


def revisions(database: Path) -> tuple[str, ...]:
    if not database.exists():
        return ()
    with closing(
        sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    ) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if ("alembic_version",) not in tables:
            if tables:
                raise ValueError(
                    "Existing database has no Alembic revision; refusing to initialize it."
                )
            return ()
        return tuple(
            row[0]
            for row in connection.execute("SELECT version_num FROM alembic_version")
        )


def backup_database(database: Path) -> DatabaseBackup:
    if not database.exists():
        return DatabaseBackup(database=database, backup=None)
    root = Path(os.environ.get("OCTOMATE_DEPLOYMENT_ROOT", str(Path.cwd())))
    if not root.is_absolute():
        raise ValueError("OCTOMATE_DEPLOYMENT_ROOT must be absolute.")
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="octomate-", suffix=".sqlite3", dir=backups
    )
    os.close(descriptor)
    destination = Path(name)
    with (
        closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
    return DatabaseBackup(database=database, backup=destination)


def migrate(backup: DatabaseBackup) -> None:
    database = database_path()
    if database != backup.database:
        raise ValueError(
            "The database target changed after backup; refusing to migrate."
        )
    ini = ALEMBIC_INI
    head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    if head is None:
        raise ValueError("No Alembic migration head exists.")
    if revisions(database) == (head,):
        print(f"Database is current at {head}.")
        return
    if backup.backup is None:
        if database.exists():
            raise ValueError(
                "A database appeared after preparation; back it up before migrating."
            )
        database.parent.mkdir(parents=True, exist_ok=True)
    else:
        snapshot = backup.backup.resolve(strict=True)
        if snapshot == database or snapshot.samefile(database):
            raise ValueError("The backup must be a separate file from production.")
        with tempfile.TemporaryDirectory(
            prefix="migration-", dir=snapshot.parent
        ) as directory:
            rehearsal = Path(directory) / "rehearsal.sqlite3"
            shutil.copyfile(snapshot, rehearsal)
            if rehearsal.resolve() == database or rehearsal.samefile(database):
                raise ValueError(
                    "Migration rehearsal resolved to the production database."
                )
            subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
                env={
                    **os.environ,
                    "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{rehearsal}",
                },
                check=True,
            )
            if revisions(rehearsal) != (head,):
                raise ValueError(
                    "Migration rehearsal did not reach the expected revision."
                )
            with closing(sqlite3.connect(rehearsal)) as connection:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError(
                        "Migration rehearsal failed SQLite integrity checks."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Migration rehearsal left invalid foreign keys.")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        check=True,
    )
    if revisions(database) != (head,):
        raise ValueError("Database did not reach the expected Alembic revision.")
    print(f"Database upgraded to {head}.")


async def verify(config: OctomateConfig) -> None:
    url = f"http://{config.host}:{config.port}"
    console_enabled = any(
        channel.enabled and channel.type == "trunkline"
        for channel in config.channels.values()
    )
    console_status = (
        (401 if config.auth is not None else 503) if console_enabled else 404
    )
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        status = (
            await client.post(
                OCTOMATE_MCP_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        ).status_code
        if status != 401:
            raise ValueError(
                f"The MCP endpoint returned {status}; expected 401 without credentials."
            )
        for path in ("/api/trunkline/health", "/api/trunkline/threads"):
            status = (await client.get(path)).status_code
            if status != console_status:
                raise ValueError(
                    f"The console route {path} returned {status}; "
                    f"expected {console_status}."
                )
    print(
        "Verified protected local Octomate MCP and "
        f"{'enabled' if console_enabled else 'disabled'} console routes."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("prepare", "check", "ready", "stopped", "backup", "migrate", "verify"),
    )
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--agent", action="append", choices=("claude", "codex", "deepseek")
    )
    parser.add_argument(
        "--channel",
        action="append",
        choices=("slack", "lark", "discord", "trunkline"),
        default=[],
    )
    parser.add_argument(
        "--mcp-presets",
        action="store_true",
        help="Read MCP preset selections as JSON from stdin.",
    )
    args = parser.parse_args()
    action = args.action
    if action == "prepare":
        if args.port is None or not 1 <= args.port <= 65535:
            parser.error("prepare requires --port between 1 and 65535")
        if not args.agent:
            parser.error("prepare requires at least one --agent")
        mcps = (
            TypeAdapter(list[McpPreset]).validate_json(sys.stdin.read())
            if args.mcp_presets
            else []
        )
        prepare(args.port, args.channel, args.agent, mcps)
        return
    if args.port is not None or args.agent or args.channel or args.mcp_presets:
        parser.error(
            "--port, --agent, --channel and --mcp-presets apply only to prepare"
        )
    config = OctomateConfig()
    database = database_path()
    if not isinstance(config.host, IPv4Address) or config.host.is_unspecified:
        raise ValueError("The managed server requires an explicit IPv4 bind address.")
    if action == "ready":
        head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
        if head is None or revisions(database) != (head,):
            raise ValueError(
                "The database schema is not current; deploy or upgrade before starting."
            )
        print(f"Database is current at {head}.")
    elif action in {"backup", "stopped"}:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind((str(config.host), config.port))
                break
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "The server did not stop within 30 seconds."
                    ) from error
                time.sleep(0.25)
        if action == "backup":
            print(backup_database(database).model_dump_json())
        else:
            print("The service port is available.")
    elif action == "migrate":
        migrate(DatabaseBackup.model_validate_json(sys.stdin.read()))
    elif action == "verify":
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(
                    (str(config.host), config.port), timeout=1
                ):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "The local server did not listen within 30 seconds."
                    ) from None
                time.sleep(0.25)
        asyncio.run(verify(config))


if __name__ == "__main__":
    main()
