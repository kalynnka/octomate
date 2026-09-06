"""Maintenance in the service environment, with a fresh interpreter after upgrades."""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from ipaddress import IPv4Address
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from octomate_protocol.deployment import DatabaseBackup
from sqlalchemy.engine import make_url

from octomate.config import OctomateConfig
from octomate.config.database import database_settings
from octomate.mcp.server import OCTOMATE_MCP_PATH

ALEMBIC_INI = Path(__file__).parent / "migrations" / "alembic.ini"


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
    backups = Path.cwd().parent / "backups"
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


def local_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """Keep MCP readiness requests out of the operator's HTTP proxies."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=follow_redirects,
        trust_env=False,
    )


async def verify(config: OctomateConfig) -> None:
    secret = next(
        user.secret for user in config.users.values() if user.secret is not None
    )
    url = f"http://{config.host}:{config.port}"
    transport = StreamableHttpTransport(
        f"{url}{OCTOMATE_MCP_PATH}",
        auth=secret.get_secret_value(),
        httpx_client_factory=local_http_client,
    )
    async with Client(transport, timeout=10, init_timeout=10) as client:
        tools = await client.list_tools()
    console_enabled = any(
        channel.enabled and channel.type == "trunkline"
        for channel in config.channels.values()
    )
    expected_status = 200 if console_enabled else 404
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        for path in ("/api/trunkline/health", "/api/trunkline/threads"):
            status = (await client.get(path)).status_code
            if status != expected_status:
                raise ValueError(
                    f"The console route {path} returned {status}; "
                    f"expected {expected_status}."
                )
    print(
        f"Verified local Octomate MCP ({len(tools)} tools) and "
        f"{'enabled' if console_enabled else 'disabled'} console routes."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "backup", "migrate", "verify"))
    action = parser.parse_args().action
    config = OctomateConfig()
    database = database_path()
    if not isinstance(config.host, IPv4Address) or config.host.is_unspecified:
        raise ValueError("The managed server requires an explicit IPv4 bind address.")
    if not any(user.secret is not None for user in config.users.values()):
        raise ValueError(
            "Register a user's bearer in users.yaml before starting the server."
        )
    if action == "backup":
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
        print(backup_database(database).model_dump_json())
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
