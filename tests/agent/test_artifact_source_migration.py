"""Content-source migration preserves installed apps and refuses snapshot loss."""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.database import database_settings
from octomate.database import async_session
from octomate.schemas.artifacts import InstalledMcpAppSource, McpAppArtifact
from octomate.schemas.thread import ThreadArtifact
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.agent.test_mcp_app_ledger import app_entry as app_entry

PREVIOUS = "af0f6303cafe"
REVISION = "ad5c96d8b83a"


@pytest.fixture
async def migration_copy(
    in_memory_engine: AsyncEngine,
    app_artifact: McpAppArtifact,
    app_entry: ThreadArtifact,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, Path, str]:
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
    assert isinstance(app_artifact.source, InstalledMcpAppSource)
    legacy = app_artifact.invocation.model_dump(mode="json", by_alias=True)
    legacy.update(app_artifact.source.model_dump(mode="json", exclude={"kind"}))
    encoded = json.dumps(legacy)
    source = await asyncio.to_thread(
        Path(in_memory_engine.url.database or "").resolve, strict=True
    )
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("UPDATE artifacts SET invocation=?", (encoded,))
        copied.execute("ALTER TABLE artifacts DROP COLUMN source")
    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    actual = await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve, strict=True
    )
    assert actual == await asyncio.to_thread(target.resolve)
    assert actual != source
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    return config, target, encoded


def test_installed_apps_roundtrip_without_losing_invocation_or_references(
    migration_copy: tuple[Config, Path, str], app_artifact: McpAppArtifact
) -> None:
    config, target, encoded = migration_copy
    command.stamp(config, PREVIOUS)
    command.upgrade(config, REVISION)
    with sqlite3.connect(target) as copied:
        invocation, source = copied.execute(
            "SELECT invocation, source FROM artifacts"
        ).fetchone()
        assert json.loads(invocation) == app_artifact.invocation.model_dump(
            mode="json", by_alias=True
        )
        assert json.loads(source) == app_artifact.source.model_dump(mode="json")
        assert copied.execute(
            "SELECT artifact_id FROM thread_messages WHERE kind='artifact'"
        ).fetchall() == [(app_artifact.id.hex,)]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert json.loads(
            copied.execute("SELECT invocation FROM artifacts").fetchone()[0]
        ) == json.loads(encoded)
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_refuses_captured_apps_before_changing_data(
    migration_copy: tuple[Config, Path, str],
) -> None:
    config, target, _ = migration_copy
    command.stamp(config, PREVIOUS)
    command.upgrade(config, REVISION)
    captured = json.dumps({"kind": "captured", "file_id": str(uuid7()), "ui": {}})
    with sqlite3.connect(target) as copied:
        copied.execute("UPDATE artifacts SET source=?", (captured,))
        before = copied.execute("SELECT * FROM artifacts").fetchall()
    with pytest.raises(RuntimeError, match="captured MCP apps exist"):
        command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert copied.execute("SELECT * FROM artifacts").fetchall() == before
        assert copied.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
