"""Compaction storage can be added without rewriting the existing archive."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from arcanus import RelationCollection
from arcanus.materia.sqlalchemy import AsyncSession, selectinload
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from uuid_utils.compat import uuid7

from octomate.config.database import database_settings
from octomate.database import async_session, create_engine
from octomate.managers import ConversationManager
from octomate.schemas.compactions import Compaction, ExternalCompaction
from octomate.schemas.conversation import ConversationCompaction
from octomate.schemas.messages import ModelRequest as StoredModelRequest
from octomate.schemas.messages import ModelResponse as StoredModelResponse
from octomate.schemas.thread import MessageBinding, ThreadMessage
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread

PREVIOUS = "792e14ef4f19"


async def test_compaction_migration_preserves_history_on_a_copy(
    in_memory_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="inkling")
    run = await manager.record_agent_run(
        conversation,
        run_id=str(uuid7()),
        messages=[
            ModelRequest(parts=[UserPromptPart("original prompt")]),
            ModelResponse(parts=[TextPart("original answer")]),
        ],
    )
    assert run is not None
    sender = UserProfile(channel_tentacle_id="test", channel_user_id="user")
    prompt = ThreadMessage(
        thread_id=conversation.thread_id,
        direction="inbound",
        actor_kind="human",
        sender_id=sender.id,
        message_text="original prompt",
    )
    async with async_session() as session:
        session.add(sender)
        await session.flush()
        session.add(prompt)
        await session.flush()
        session.add(
            MessageBinding(
                thread_message_id=prompt.id,
                model_message_id=run.messages[0].id,
                run_id=run.id,
                kind="request_source",
            )
        )
        await session.commit()
    source = await asyncio.to_thread(Path(in_memory_engine.url.database or "").resolve)
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "migration-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        tables = (
            "conversations",
            "agent_runs",
            "conversation_runs",
            "model_messages",
            "thread_messages",
            "message_binding",
        )

    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    assert await asyncio.to_thread(
        Path(make_url(database_settings.db_url).database or "").resolve
    ) == await asyncio.to_thread(target.resolve)
    assert source != target
    module_path = await asyncio.to_thread(Path(__file__).resolve)
    config = Config(str(module_path.parents[2] / "alembic.ini"))
    monkeypatch.setattr("logging.config.fileConfig", Mock())

    def remove_compaction_storage() -> None:
        # Remove this branch directly; the sibling ownership migration is irreversible.
        command.downgrade(config, PREVIOUS)
        migration = ScriptDirectory.from_config(config).get_revision("1f46faa4d027")
        assert migration is not None
        engine = create_sync_engine(f"sqlite:///{target}")
        try:
            with (
                engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                migration.module.downgrade()
        finally:
            engine.dispose()
        command.stamp(config, PREVIOUS, purge=True)

    await asyncio.to_thread(command.stamp, config, "head")
    await asyncio.to_thread(remove_compaction_storage)
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        # Compare the historical columns, even when today's model has newer ones.
        columns = {
            table: ", ".join(
                f'"{row[1]}"' for row in copied.execute(f'PRAGMA table_info("{table}")')
            )
            for table in tables
        }
        before = {
            table: original.execute(
                f'SELECT {columns[table]} FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }
        assert {
            table: copied.execute(
                f'SELECT {columns[table]} FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        } == before
    await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.check, config)
    with sqlite3.connect(target) as copied:
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            table: copied.execute(
                f'SELECT {columns[table]} FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        } == before

    engine = create_engine(database_settings.db_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        owned = Compaction(
            conversation_id=conversation.id,
            messages=RelationCollection(
                [StoredModelRequest(parts=[UserPromptPart("replacement context")])]
            ),
        )
        external = ExternalCompaction(
            conversation_id=conversation.id,
            source="session.jsonl",
            end_offset=128,
            messages=RelationCollection(
                [StoredModelResponse(parts=[TextPart("external summary")])]
            ),
        )
        async with maker() as session:
            session.add_all([owned, external])
            await session.commit()
        async with maker() as session:
            rows = await session.list(
                Compaction,
                order_bys=[Compaction["id"]],
                options=[selectinload(Compaction["messages"])],
            )
        assert {row.id: row.model_dump() for row in rows} == {
            row.id: row.model_dump() for row in (owned, external)
        }
        assert any(isinstance(row, ExternalCompaction) for row in rows)
        assert [message.parts for row in rows for message in row.messages] == [
            message.parts for row in (owned, external) for message in row.messages
        ]
        async with maker() as session:
            session.add(StoredModelRequest(parts=[UserPromptPart("unowned")]))
            with pytest.raises(IntegrityError, match="ck_model_messages_single_owner"):
                await session.commit()
        with pytest.raises(RuntimeError, match="compaction-owned messages exist"):
            await asyncio.to_thread(remove_compaction_storage)
        await asyncio.to_thread(command.upgrade, config, "head")
        async with maker() as session:
            preserved = await session.list(
                Compaction,
                order_bys=[Compaction["id"]],
                options=[selectinload(Compaction["messages"])],
            )
        assert [message.id for row in preserved for message in row.messages] == [
            message.id for row in rows for message in row.messages
        ]
        async with maker() as session:
            for link in await session.list(ConversationCompaction):
                await session.delete(link)
            await session.flush()
            for row in rows:
                await session.delete(row)
            await session.commit()
    finally:
        await engine.dispose()

    await asyncio.to_thread(remove_compaction_storage)
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
