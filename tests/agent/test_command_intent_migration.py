"""Renaming command intent preserves ledger contents and history-search triggers."""

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

from octomate.config.database import database_settings
from octomate.database import async_session
from octomate.schemas.commands import CommandResult
from octomate.schemas.thread import ThreadCommand
from tests.agent.test_command_ledger import receipt as receipt

PREVIOUS = "d6ddcf644273"
REVISION = "4681c219c827"


@pytest.mark.parametrize("completed", [False, True])
async def test_command_intent_column_roundtrip_preserves_ledger_and_search(
    in_memory_engine: AsyncEngine,
    receipt: ThreadCommand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    receipt.outcome = CommandResult() if completed else None
    async with async_session() as session:
        session.add(receipt)
        await session.commit()
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
        copied.execute("ALTER TABLE thread_messages RENAME COLUMN intent TO invocation")
        before = copied.execute("SELECT * FROM thread_messages ORDER BY id").fetchall()
        triggers = copied.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='thread_messages' ORDER BY name"
        ).fetchall()
        search = copied.execute(
            "SELECT message_id, message_text FROM thread_messages_fts ORDER BY message_id"
        ).fetchall()
    assert len(triggers) == 3
    assert len(before) == 2

    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    actual = await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve, strict=True
    )
    assert actual == await asyncio.to_thread(target.resolve)
    assert actual != source
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    await asyncio.to_thread(command.stamp, config, PREVIOUS)

    for migrate, revision, column, removed in (
        (command.upgrade, REVISION, "intent", "invocation"),
        (command.downgrade, PREVIOUS, "invocation", "intent"),
    ):
        await asyncio.to_thread(migrate, config, revision)
        with sqlite3.connect(target) as copied:
            copied.execute("PRAGMA foreign_keys=ON")
            columns = {
                row[1] for row in copied.execute("PRAGMA table_info(thread_messages)")
            }
            assert column in columns
            assert removed not in columns
            assert (
                copied.execute("SELECT * FROM thread_messages ORDER BY id").fetchall()
                == before
            )
            encoded = copied.execute(
                f'SELECT "{column}" FROM thread_messages WHERE id=?', (receipt.id.hex,)
            ).fetchone()[0]
            assert json.loads(encoded) == receipt.intent.model_dump(mode="json")
            assert (
                copied.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='thread_messages' ORDER BY name"
                ).fetchall()
                == triggers
            )
            assert (
                copied.execute(
                    "SELECT message_id, message_text FROM thread_messages_fts ORDER BY message_id"
                ).fetchall()
                == search
            )
            assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
            copied.execute(
                "UPDATE thread_messages SET message_text=? WHERE id=?",
                ("searchable rename sentinel", receipt.id.hex),
            )
            assert copied.execute(
                "SELECT message_id FROM thread_messages_fts "
                "WHERE thread_messages_fts MATCH 'sentinel'"
            ).fetchall() == [(receipt.id.hex,)]
            copied.rollback()
