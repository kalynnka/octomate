"""Native ownership migrates in place while transport and history stay intact."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def migration_foreign_keys() -> Iterator[None]:
    def enforce_foreign_keys(
        connection: DBAPIConnection, _: ConnectionPoolEntry
    ) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(Engine, "connect", enforce_foreign_keys)
    try:
        yield
    finally:
        event.remove(Engine, "connect", enforce_foreign_keys)


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
    command.upgrade(case.config, REVISION)
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


def test_different_native_sessions_refuse_without_merging_history(
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
        row[columns.index("external_id")] = "different-session"
        copied.execute(
            f"INSERT INTO conversations VALUES ({','.join('?' for _ in columns)})", row
        )
        before = "\n".join(copied.iterdump())
    with pytest.raises(RuntimeError, match="different native sessions"):
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
def test_identical_native_addresses_merge(
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
        target_id = uuid7().hex
        row[columns.index("id")] = target_id
        row[columns.index("channel_tentacle_id")] = case.agent_id
        copied.execute(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", row
        )
        if table == "threads":
            copied.execute(
                "INSERT INTO thread_ledgers VALUES (?, ?, NULL)", (target_id, target_id)
            )
    command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as copied:
        assert copied.execute(
            f"SELECT id FROM {table} WHERE channel_tentacle_id=?", (case.agent_id,)
        ).fetchall() == [(target_id,)]
        assert copied.execute("PRAGMA foreign_key_check").fetchall() == []


def clone_row(
    connection: sqlite3.Connection,
    table: str,
    source_id: str,
    **changes: str | int | None,
) -> str:
    cursor = connection.execute(f"SELECT * FROM {table} WHERE id=?", (source_id,))
    assert cursor.description is not None
    values = dict(
        zip(
            (column[0] for column in cursor.description), cursor.fetchone(), strict=True
        )
    )
    new_id = uuid7().hex
    values.update(id=new_id, **changes)
    connection.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        list(values.values()),
    )
    return new_id


@dataclass
class DuplicateHistory:
    case: MigrationCopy
    source_thread: str
    target_thread: str
    source_conversation: str
    target_conversation: str
    source_message: str
    target_message: str
    source_profile: str
    target_profile: str


@pytest.fixture
def duplicate_history(migration_copy: MigrationCopy) -> DuplicateHistory:
    case = migration_copy
    with sqlite3.connect(case.target) as db:
        db.execute("PRAGMA foreign_keys=ON")
        source_thread = db.execute(
            "SELECT id FROM threads WHERE channel_tentacle_id=?", (case.native_id,)
        ).fetchone()[0]
        target_thread = clone_row(
            db, "threads", source_thread, channel_tentacle_id=case.agent_id
        )
        db.execute(
            "INSERT INTO thread_ledgers VALUES (?, ?, NULL)",
            (target_thread, target_thread),
        )
        source_conversation, source_file = db.execute(
            "SELECT id, transcript_file_id FROM conversations WHERE thread_id=? AND subagent_id=''",
            (source_thread,),
        ).fetchone()
        target_file = clone_row(db, "files", source_file, key="latest-transcript")
        target_conversation = clone_row(
            db,
            "conversations",
            source_conversation,
            thread_id=target_thread,
            agent_tentacle_id=case.agent_id,
            transcript_file_id=target_file,
        )
        source_run = db.execute(
            "SELECT id FROM agent_runs WHERE conversation_id=?", (source_conversation,)
        ).fetchone()[0]
        target_run = clone_row(
            db,
            "agent_runs",
            source_run,
            conversation_id=target_conversation,
            name=case.agent_id,
            start_offset=100,
            end_offset=200,
        )
        db.executemany(
            "INSERT INTO conversation_runs VALUES (?, ?)",
            [(target_conversation, source_run), (target_conversation, target_run)],
        )
        source_model = db.execute(
            "SELECT id FROM model_messages WHERE run_id=?", (source_run,)
        ).fetchone()[0]
        target_model = clone_row(db, "model_messages", source_model, run_id=target_run)
        source_message, source_profile, created = db.execute(
            "SELECT id,sender_id,created_at FROM thread_messages WHERE thread_id=?",
            (source_thread,),
        ).fetchone()
        target_profile = clone_row(
            db, "user_profiles", source_profile, channel_tentacle_id=case.agent_id
        )
        db.execute(
            "UPDATE thread_messages SET platform_message_id='shared' WHERE id=?",
            (source_message,),
        )
        target_message = clone_row(
            db,
            "thread_messages",
            source_message,
            thread_id=target_thread,
            sender_id=target_profile,
            agent_tentacle_id=case.agent_id,
        )
        clone_row(db, "thread_messages", source_message, platform_message_id="old-only")
        clone_row(db, "thread_messages", target_message, platform_message_id="new-only")
        db.execute(
            "UPDATE threads SET source_cursor_message_id=? WHERE id=?",
            (source_message, source_thread),
        )
        db.executemany(
            "INSERT INTO message_binding (thread_message_id,model_message_id,kind,run_id,tool_call_id,position,created_at) VALUES (?,?,'reply',?,NULL,0,?)",
            [
                (source_message, source_model, source_run, created),
                (target_message, source_model, source_run, created),
                (source_message, target_model, target_run, created),
            ],
        )
        for conversation_id, ref in [
            (source_conversation, "old-todo"),
            (target_conversation, "new-todo"),
            (source_conversation, "shared-todo"),
            (target_conversation, "shared-todo"),
        ]:
            db.execute(
                "INSERT INTO todos (id,conversation_id,ref,content,active_form,status,position,parent_ref,depends_on,created_at,updated_at) VALUES (?,?,?,'Keep the history','','pending',0,NULL,'[]',?,?)",
                (uuid7().hex, conversation_id, ref, created, created),
            )
    return DuplicateHistory(
        case,
        source_thread,
        target_thread,
        source_conversation,
        target_conversation,
        source_message,
        target_message,
        source_profile,
        target_profile,
    )


def test_duplicate_histories_keep_runs_messages_bindings_and_references(
    duplicate_history: DuplicateHistory,
) -> None:
    history = duplicate_history
    case = history.case
    with sqlite3.connect(case.target) as db:
        runs = set(db.execute("SELECT id FROM agent_runs"))
        model_messages = db.execute(
            "SELECT * FROM model_messages ORDER BY id"
        ).fetchall()
        files = db.execute("SELECT * FROM files ORDER BY id").fetchall()
        transcript = db.execute(
            "SELECT transcript_file_id FROM conversations WHERE id=?",
            (history.target_conversation,),
        ).fetchone()
    command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as db:
        assert set(db.execute("SELECT id FROM agent_runs")) == runs
        assert (
            db.execute("SELECT * FROM model_messages ORDER BY id").fetchall()
            == model_messages
        )
        assert db.execute("SELECT * FROM files ORDER BY id").fetchall() == files
        assert db.execute(
            "SELECT id FROM threads WHERE channel_tentacle_id=?", (case.agent_id,)
        ).fetchall() == [(history.target_thread,)]
        assert (
            db.execute(
                "SELECT transcript_file_id FROM conversations WHERE id=?",
                (history.target_conversation,),
            ).fetchone()
            == transcript
        )
        assert db.execute(
            "SELECT source_cursor_message_id FROM threads WHERE id=?",
            (history.target_thread,),
        ).fetchone() == (history.target_message,)
        assert set(db.execute("SELECT conversation_id FROM agent_runs")) == {
            (history.target_conversation,)
        }
        assert (
            set(
                db.execute(
                    "SELECT run_id FROM conversation_runs WHERE conversation_id=?",
                    (history.target_conversation,),
                )
            )
            == runs
        )
        assert db.execute(
            "SELECT parent_conversation_id FROM conversations WHERE subagent_id='child'"
        ).fetchone() == (history.target_conversation,)
        assert db.execute(
            "SELECT source_conversation_id,target_conversation_id,thread_id FROM channel_handoffs"
        ).fetchone() == (
            history.target_conversation,
            history.target_conversation,
            history.target_thread,
        )
        assert db.execute(
            "SELECT conversation_id FROM deferred_action_batches"
        ).fetchone() == (history.target_conversation,)
        assert set(
            db.execute(
                "SELECT thread_id,sender_id,platform_message_id FROM thread_messages"
            )
        ) == {
            (history.target_thread, history.target_profile, key)
            for key in ("shared", "old-only", "new-only")
        }
        assert db.execute(
            "SELECT thread_message_id,count(*) FROM message_binding GROUP BY thread_message_id"
        ).fetchall() == [(history.target_message, 2)]
        assert set(db.execute("SELECT conversation_id,ref FROM todos")) == {
            (history.target_conversation, key)
            for key in ("old-todo", "new-todo", "shared-todo")
        }
        assert db.execute(
            "SELECT * FROM thread_ledgers WHERE thread_id=?", (history.target_thread,)
        ).fetchall() == [(history.target_thread, history.target_thread, None)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("conflict", ["message", "binding", "session", "todo", "fork"])
def test_duplicate_history_conflicts_roll_back_every_merge(
    duplicate_history: DuplicateHistory,
    conflict: str,
) -> None:
    history = duplicate_history
    case = history.case
    with sqlite3.connect(case.target) as db:
        if conflict == "message":
            db.execute(
                "UPDATE thread_messages SET message_text='Different answer' WHERE id=?",
                (history.target_message,),
            )
        elif conflict == "binding":
            db.execute(
                "UPDATE message_binding SET position=1 WHERE thread_message_id=?",
                (history.target_message,),
            )
        elif conflict == "session":
            db.execute(
                "UPDATE conversations SET external_id='different-session' WHERE id=?",
                (history.target_conversation,),
            )
        elif conflict == "todo":
            db.execute(
                "UPDATE todos SET content='Different task' WHERE conversation_id=? AND ref='shared-todo'",
                (history.target_conversation,),
            )
        else:
            fork = db.execute(
                "SELECT thread_id FROM conversations WHERE agent_tentacle_id='coding'"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO thread_ledgers VALUES (?, ?, ?)",
                (fork, history.source_thread, history.source_message),
            )
        db.commit()
        before = "\n".join(db.iterdump())
    with pytest.raises(RuntimeError, match="Cannot merge"):
        command.upgrade(case.config, REVISION)
    with sqlite3.connect(case.target) as db:
        assert "\n".join(db.iterdump()) == before
