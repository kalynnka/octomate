"""The routing migration preserves actions and separates their continuation paths."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.database import database_settings
from tests.agent.test_deferred_actions import _create_batch


async def test_migration_backfills_response_modes_on_a_copy(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = await _create_batch("live")
    resumable = await _create_batch("resume")
    source = await asyncio.to_thread(Path(in_memory_engine.url.database or "").resolve)
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("ALTER TABLE deferred_action_batches DROP COLUMN response_mode")
        before_batches = copied.execute(
            "SELECT * FROM deferred_action_batches ORDER BY id"
        ).fetchall()
        before_actions = copied.execute(
            "SELECT * FROM deferred_actions ORDER BY id"
        ).fetchall()
        columns = [
            row[1]
            for row in copied.execute("PRAGMA table_info(deferred_action_batches)")
        ]

    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    assert await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve
    ) == await asyncio.to_thread(target.resolve)
    assert source != target
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    # Alembic's CLI logging setup disables pytest's existing application loggers.
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    await asyncio.to_thread(command.stamp, config, "cfecde28ca54")
    await asyncio.to_thread(command.upgrade, config, "39d7ab82a352")

    with sqlite3.connect(target) as copied:
        modes = dict(
            copied.execute("SELECT id, response_mode FROM deferred_action_batches")
        )
        assert modes == {live.id.hex: "live", resumable.id.hex: "resume"}
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        names = ", ".join(f'"{name}"' for name in columns)
        assert (
            copied.execute(
                f"SELECT {names} FROM deferred_action_batches ORDER BY id"
            ).fetchall()
            == before_batches
        )
        assert (
            copied.execute("SELECT * FROM deferred_actions ORDER BY id").fetchall()
            == before_actions
        )
