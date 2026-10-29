"""Adding response metadata preserves existing history on a disposable copy."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.database import database_settings
from octomate.managers import ConversationManager
from tests.support.managers import a_thread


async def test_response_metadata_migration_preserves_history_on_a_copy(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[
            ModelRequest(parts=[UserPromptPart("hello")]),
            ModelResponse(parts=[TextPart("hi")]),
        ],
    )
    source = await asyncio.to_thread(Path(in_memory_engine.url.database or "").resolve)
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("ALTER TABLE model_messages DROP COLUMN history_digest")
        copied.execute("ALTER TABLE model_messages DROP COLUMN failed_attempts")
        before = copied.execute("SELECT * FROM model_messages ORDER BY id").fetchall()
        columns = [
            row[1] for row in copied.execute("PRAGMA table_info(model_messages)")
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
    await asyncio.to_thread(command.stamp, config, "ad5c96d8b83a")
    await asyncio.to_thread(command.upgrade, config, "294f429c72cb")

    with sqlite3.connect(target) as copied:
        assert copied.execute(
            "SELECT history_digest, failed_attempts FROM model_messages"
        ).fetchall() == [(None, None), (None, None)]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        names = ", ".join(f'"{name}"' for name in columns)
        assert (
            copied.execute(f"SELECT {names} FROM model_messages ORDER BY id").fetchall()
            == before
        )
