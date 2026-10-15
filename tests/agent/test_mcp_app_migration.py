"""Adding app invocation storage preserves the ledger and its search triggers."""

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.database import database_settings
from octomate.schemas.thread import ThreadMcpAppInvocation
from tests.agent.test_mcp_app_ledger import app_entry as app_entry

PREVIOUS = "741f7a1817c2"
REVISION = "4321bb383143"


@pytest.fixture
def migration_copy(
    in_memory_engine: AsyncEngine,
    app_entry: ThreadMcpAppInvocation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, Path]:
    source = Path(in_memory_engine.url.database or "").resolve(strict=True)
    assert source.is_relative_to(tmp_path.resolve())
    target = tmp_path / "migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("ALTER TABLE thread_messages DROP COLUMN app_invocation")
    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    actual = Path(make_url(database_settings.db_url).database or "").resolve(
        strict=True
    )
    assert actual == target.resolve()
    assert actual != source
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    command.stamp(config, PREVIOUS)
    return config, target


def test_migration_preserves_history_and_triggers(
    migration_copy: tuple[Config, Path],
) -> None:
    config, target = migration_copy
    with sqlite3.connect(target) as copied:
        columns = [
            row[1] for row in copied.execute("PRAGMA table_info(thread_messages)")
        ]
        names = ", ".join(f'"{name}"' for name in columns)
        before = copied.execute(
            f"SELECT {names} FROM thread_messages ORDER BY id"
        ).fetchall()
        triggers = copied.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    command.upgrade(config, REVISION)
    with sqlite3.connect(target) as copied:
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        assert copied.execute(
            "SELECT app_invocation FROM thread_messages"
        ).fetchall() == [(None,)]
        assert (
            copied.execute(
                f"SELECT {names} FROM thread_messages ORDER BY id"
            ).fetchall()
            == before
        )
    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            copied.execute(
                f"SELECT {names} FROM thread_messages ORDER BY id"
            ).fetchall()
            == before
        )
        assert (
            copied.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            ).fetchall()
            == triggers
        )


def test_downgrade_refuses_to_discard_app_history(
    migration_copy: tuple[Config, Path],
    app_entry: ThreadMcpAppInvocation,
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    invocation = (app_entry.app_invocation.model_dump_json(),)
    with sqlite3.connect(target) as copied:
        copied.execute(
            "UPDATE thread_messages SET kind = 'mcp_app', app_invocation = ?",
            invocation,
        )
    with pytest.raises(RuntimeError, match="MCP app ledger entries exist"):
        command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert copied.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
        assert (
            copied.execute("SELECT app_invocation FROM thread_messages").fetchone()
            == invocation
        )
