"""Artifact storage preserves history, references, and search triggers."""

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.database import database_settings
from octomate.schemas.artifacts import McpAppArtifact
from octomate.schemas.thread import ThreadArtifact
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.agent.test_mcp_app_ledger import app_entry as app_entry

PREVIOUS = "741f7a1817c2"
REVISION = "642099f8b906"


@pytest.fixture
def migration_copy(
    in_memory_engine: AsyncEngine,
    app_entry: ThreadArtifact,
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
    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    actual = Path(make_url(database_settings.db_url).database or "").resolve(
        strict=True
    )
    assert actual == target.resolve()
    assert actual != source
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    command.stamp(config, REVISION)
    command.downgrade(config, PREVIOUS)
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
        assert (
            copied.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            ).fetchall()
            == triggers
        )
        assert copied.execute("SELECT artifact_id FROM thread_messages").fetchall() == [
            (None,)
        ]
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


@pytest.mark.parametrize("referenced", [False, True])
def test_downgrade_refuses_to_discard_artifacts(
    migration_copy: tuple[Config, Path],
    app_artifact: McpAppArtifact,
    referenced: bool,
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    invocation = app_artifact.invocation.model_dump_json()
    with sqlite3.connect(target) as copied:
        copied.execute("PRAGMA foreign_keys = ON")
        copied.execute(
            "INSERT INTO artifacts (id, owner_id, kind, title, created_at, invocation) VALUES (?, ?, ?, ?, ?, ?)",
            (
                app_artifact.id.hex,
                app_artifact.owner_id.hex,
                app_artifact.kind,
                app_artifact.title,
                app_artifact.created_at.isoformat(),
                invocation,
            ),
        )
        if referenced:
            copied.execute(
                "UPDATE thread_messages SET kind = 'artifact', artifact_id = ?",
                (app_artifact.id.hex,),
            )
    with pytest.raises(RuntimeError, match="artifacts exist"):
        command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert copied.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
        assert copied.execute("SELECT invocation FROM artifacts").fetchone() == (
            invocation,
        )
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
