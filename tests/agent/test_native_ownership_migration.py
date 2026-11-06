"""Native ownership migrates in place while transport and history stay intact."""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import ConnectionPoolEntry
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.database import database_settings
from octomate.database import async_session
from octomate.schemas.conversation import Conversation
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.files import Jsonl
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import Handoff, ThreadKey
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread

PREVIOUS = "1a83da0aa445"
REVISION = "792e14ef4f19"
OWNERSHIP_COLUMNS = {
    "agent_runs": {"name"},
    "conversations": {"agent_tentacle_id"},
    "thread_messages": {"agent_tentacle_id"},
    "channel_handoffs": {"source_agent_tentacle_id", "to_agent_tentacle_id"},
    "deferred_action_batches": {"agent_tentacle_id"},
    "threads": {"channel_tentacle_id"},
    "user_profiles": {"channel_tentacle_id"},
}


@dataclass
class MigrationCopy:
    config: Config
    source: Path
    target: Path
    native_id: str
    agent_id: str


@pytest.fixture(
    params=[
        ("codex-native", "codex"),
        ("claude-native", "claude"),
        ("deepseek-native", "deepseek"),
    ]
)
async def migration_copy(
    request: pytest.FixtureRequest,
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MigrationCopy:
    native_id, agent_id = request.param
    host = Octomate()
    thread = await host.threads.ensure(
        ThreadKey(native_id, "thread", "session"), native=True
    )
    parent = await host.conversations.ensure(thread.id, agent_tentacle_id=native_id)
    await host.conversations.ensure(
        thread.id,
        agent_tentacle_id=native_id,
        subagent_id="child",
        parent_conversation_id=parent.id,
    )
    await host.conversations.ensure(
        await a_thread("driven"), agent_tentacle_id="coding"
    )
    await host.conversations.record_external_run(
        parent,
        run_id=str(uuid7()),
        native_session_id="session",
        name=native_id,
        messages=[ModelResponse(parts=[TextPart("Saved answer")])],
        start_offset=0,
        end_offset=100,
    )
    await host.threads.record_outbound(
        thread,
        agent_tentacle_id=native_id,
        segments=[TextSegment(data={"text": "Saved answer"})],
        sender=UserProfile(channel_user_id="owner"),
    )
    async with async_session() as session:
        transcript = Jsonl(
            name="session.jsonl", size=100, provider="filesystem", key="transcript"
        )
        session.add(transcript)
        await session.flush()
        stored = await session.get(Conversation, parent.id)
        assert stored is not None
        stored.transcript_file_id = transcript.id
        session.add(
            Handoff(
                thread_id=thread.id,
                source_agent_tentacle_id=native_id,
                to_agent_tentacle_id=native_id,
                source_conversation_id=parent.id,
                target_conversation_id=parent.id,
            )
        )
        session.add(
            DeferredActionBatch(
                conversation_id=parent.id,
                agent_tentacle_id=native_id,
                response_mode="live",
                source_address=thread.key.address("owner"),
                target_address=thread.key.address("owner"),
                requests=DeferredToolRequests(),
            )
        )
        await session.commit()
    source = await asyncio.to_thread(
        Path(in_memory_engine.url.database or "").resolve, strict=True
    )
    assert source.is_relative_to(await asyncio.to_thread(tmp_path.resolve))
    target = tmp_path / "ownership-copy.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copied,
    ):
        original.backup(copied)
        copied.execute("ALTER TABLE agent_runs ADD COLUMN native_id VARCHAR")
        copied.execute("CREATE INDEX ix_agent_runs_native_id ON agent_runs (native_id)")
        copied.execute("UPDATE agent_runs SET native_id=?", (native_id,))
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
    return MigrationCopy(config, source, target, native_id, agent_id)


def test_upgrade_preserves_history_and_removes_runtime_id(
    migration_copy: MigrationCopy,
) -> None:
    case = migration_copy

    def enforce_foreign_keys(
        connection: DBAPIConnection, _: ConnectionPoolEntry
    ) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(Engine, "connect", enforce_foreign_keys)
    try:
        command.upgrade(case.config, REVISION)
    finally:
        event.remove(Engine, "connect", enforce_foreign_keys)
    with (
        sqlite3.connect(case.source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(case.target) as copied,
    ):
        assert "native_id" not in {
            row[1] for row in copied.execute("PRAGMA table_info(agent_runs)")
        }
        assert "ix_agent_runs_native_id" not in {
            row[1] for row in copied.execute("PRAGMA index_list(agent_runs)")
        }
        for column in ("source_address", "target_address"):
            query = f"SELECT {column} FROM deferred_action_batches ORDER BY id"
            addresses = [json.loads(row[0]) for row in original.execute(query)]
            for address in addresses:
                address["channel_tentacle_id"] = case.agent_id
            assert [json.loads(row[0]) for row in copied.execute(query)] == addresses
        tables = [
            row[0]
            for row in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'thread_messages_fts%' AND name != 'alembic_version'"
            )
        ]
        for table in tables:
            ownership = OWNERSHIP_COLUMNS.get(table, set())
            columns = [
                row[1]
                for row in original.execute(f"PRAGMA table_info({table})")
                if table != "deferred_action_batches"
                or row[1] not in {"source_address", "target_address"}
            ]
            query = f"SELECT {','.join(columns)} FROM {table} ORDER BY 1, 2"
            rows = original.execute(query).fetchall()
            expected = [
                tuple(
                    case.agent_id
                    if column in ownership and value == case.native_id
                    else value
                    for column, value in zip(columns, row, strict=True)
                )
                for row in rows
            ]
            assert copied.execute(query).fetchall() == expected
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
        assert copied.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )


def test_collision_refuses_without_merging_history(
    migration_copy: MigrationCopy,
) -> None:
    case = migration_copy
    with sqlite3.connect(case.target) as copied:
        columns = [
            column[1] for column in copied.execute("PRAGMA table_info(conversations)")
        ]
        row = list(
            copied.execute(
                "SELECT * FROM conversations WHERE agent_tentacle_id=? AND subagent_id=''",
                (case.native_id,),
            ).fetchone()
        )
        row[columns.index("id")] = uuid7().hex
        row[columns.index("agent_tentacle_id")] = case.agent_id
        copied.execute(
            f"INSERT INTO conversations VALUES ({','.join('?' for _ in columns)})", row
        )
        before = "\n".join(copied.iterdump())
    with pytest.raises(RuntimeError, match="already owns that context"):
        command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        assert "\n".join(copied.iterdump()) == before


def test_failed_update_rolls_back_all_ownership(migration_copy: MigrationCopy) -> None:
    case = migration_copy
    with sqlite3.connect(case.target) as copied:
        copied.execute(
            "CREATE TRIGGER reject_owner BEFORE UPDATE OF agent_tentacle_id ON thread_messages BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
        before = "\n".join(copied.iterdump())
    with pytest.raises(IntegrityError, match="forced failure"):
        command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        assert "\n".join(copied.iterdump()) == before


def test_downgrade_refuses_to_guess_original_owners(
    migration_copy: MigrationCopy,
) -> None:
    case = migration_copy
    command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        before = "\n".join(copied.iterdump())
    with pytest.raises(RuntimeError, match="Restore a database backup"):
        command.downgrade(case.config, PREVIOUS)
    with sqlite3.connect(case.target) as copied:
        assert "\n".join(copied.iterdump()) == before


def test_failed_column_drop_rolls_back_ownership_and_index(
    migration_copy: MigrationCopy,
) -> None:
    case = migration_copy
    with sqlite3.connect(case.target) as copied:
        copied.execute("CREATE INDEX keep_runtime ON agent_runs (native_id)")
        before = "\n".join(copied.iterdump())
    with pytest.raises(OperationalError, match="keep_runtime"):
        command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        assert "\n".join(copied.iterdump()) == before


@pytest.mark.parametrize("table", ["threads", "user_profiles"])
def test_address_collision_rolls_back_without_merging_rows(
    migration_copy: MigrationCopy, table: str
) -> None:
    case = migration_copy
    with sqlite3.connect(case.target) as copied:
        columns = [row[1] for row in copied.execute(f"PRAGMA table_info({table})")]
        row = list(
            copied.execute(
                f"SELECT * FROM {table} WHERE channel_tentacle_id=?",
                (case.native_id,),
            ).fetchone()
        )
        row[columns.index("id")] = uuid7().hex
        row[columns.index("channel_tentacle_id")] = case.agent_id
        copied.execute(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", row
        )
        before = "\n".join(copied.iterdump())
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        assert "\n".join(copied.iterdump()) == before
