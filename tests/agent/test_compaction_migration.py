"""Compaction identities can be added without rewriting the existing archive."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from arcanus.materia.sqlalchemy import AsyncSession
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from uuid_utils.compat import uuid7

from octomate.config.database import database_settings
from octomate.database import create_engine
from octomate.managers import ConversationManager
from octomate.schemas.compactions import Compaction, ExternalCompaction
from tests.support.managers import a_thread

PREVIOUS = "1a83da0aa445"
REVISION = "1f46faa4d027"


async def test_compaction_migration_preserves_history_on_a_copy(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[
            ModelRequest(parts=[UserPromptPart("original prompt")]),
            ModelResponse(parts=[TextPart("original answer")]),
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
        copied.execute("DROP TABLE compactions")
        tables = ("conversations", "agent_runs", "conversation_runs", "model_messages")
        before = {
            table: copied.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in tables
        }

    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    assert await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve
    ) == await asyncio.to_thread(target.resolve)
    assert source != target
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    await asyncio.to_thread(command.stamp, config, PREVIOUS)
    await asyncio.to_thread(command.upgrade, config, REVISION)
    await asyncio.to_thread(command.check, config)
    with sqlite3.connect(target) as copied:
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []

    engine = create_engine(database_settings.db_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        owned = Compaction(conversation_id=conversation.id)
        external = ExternalCompaction(
            conversation_id=conversation.id, source="session.jsonl", end_offset=128
        )
        async with maker() as session:
            session.add_all([owned, external])
            await session.commit()
        async with maker() as session:
            rows = await session.list(Compaction)
        assert {row.id: row.model_dump() for row in rows} == {
            row.id: row.model_dump() for row in (owned, external)
        }
        assert any(isinstance(row, ExternalCompaction) for row in rows)
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, PREVIOUS)
    with sqlite3.connect(target) as copied:
        assert (
            copied.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='compactions'"
            ).fetchall()
            == []
        )
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in before.items():
            assert (
                copied.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                == rows
            )
