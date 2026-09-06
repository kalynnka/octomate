from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import subprocess
import sys
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastmcp import FastMCP
from octomate_protocol.deployment import DatabaseBackup
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from octomate import deployment
from octomate.config.database import database_settings
from octomate.mcp.base import KnownBearers
from tests.support.config import registered

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "production.sqlite3"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("OCTOMATE_DB_URL", url)
    monkeypatch.setattr(database_settings, "db_url", url)
    shutil.copyfile(ROOT / "alembic.ini", tmp_path / "alembic.ini")
    (tmp_path / "migrations").symlink_to(ROOT / "migrations", target_is_directory=True)
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


@pytest.mark.parametrize("stops", [True, False])
def test_backup_waits_for_server_shutdown(
    database: Path, monkeypatch: pytest.MonkeyPatch, stops: bool
) -> None:
    monkeypatch.setattr(sys, "argv", ["maintenance", "backup"])
    monkeypatch.setattr(deployment, "OctomateConfig", lambda: registered("test-bearer"))
    monkeypatch.setattr(deployment.time, "sleep", lambda duration: None)
    busy = OSError(errno.EADDRINUSE, "Server is still listening")
    with (
        patch("octomate.deployment.socket.socket") as sockets,
        patch(
            "octomate.deployment.time.monotonic", side_effect=[0, 0 if stops else 31]
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
        Config(str(ROOT / "alembic.ini"))
    ).get_current_head()
    assert deployment.revisions(database) == (head,)
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


def test_actual_upgrade_rehearses_on_a_copy(database: Path) -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    head = script.get_current_head()
    assert head is not None
    revision = script.get_revision(head)
    assert revision is not None
    previous = revision.down_revision
    assert isinstance(previous, str)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
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


@pytest.mark.parametrize("console_enabled", [False, True])
async def test_verification_checks_authenticated_mcp_and_console_routes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    console_enabled: bool,
) -> None:
    config = registered("test-bearer")
    server = FastMCP("gateway", auth=KnownBearers(config.users))

    @server.tool
    def hello() -> str:
        return "hello"

    api = server.http_app(path="/gateway/mcp", stateless_http=True)

    async def console(request: Request) -> Response:
        return Response("console")

    if console_enabled:
        api.routes.append(Route("/api/trunkline/threads", console))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.ASGITransport(app=api)),
    )
    async with api.lifespan(api):
        if console_enabled:
            with pytest.raises(ValueError, match="console route"):
                await deployment.verify(config)
        else:
            await deployment.verify(config)
            output = capsys.readouterr().out
            assert "1 tools" in output
            assert "test-bearer" not in output
