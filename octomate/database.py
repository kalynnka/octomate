from functools import cache

from arcanus.materia.sqlalchemy import AsyncSession
from pydantic import JsonValue
from pydantic_core import from_json, to_json
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from octomate.config.database import database_settings


def json_serializer(value: JsonValue) -> str:
    """Serialize JSON columns via pydantic, tolerating Pydantic-friendly values.

    Arcanus/Pydantic schemas own validation; the engine only prepares values for
    the database JSON serializer.
    """
    return to_json(value, bytes_mode="base64", fallback=str).decode()


def json_deserializer(value: str | bytes) -> JsonValue:
    return from_json(value)


def create_engine(url: str) -> AsyncEngine:
    """The engine every caller gets, tests included — so the constraints a test
    runs under are the ones production runs under."""
    created = create_async_engine(
        url,
        json_serializer=json_serializer,
        json_deserializer=json_deserializer,
    )
    if created.dialect.name == "sqlite":

        @event.listens_for(created.sync_engine, "connect")
        def sqlite_pragmas(dbapi_connection: DBAPIConnection, _: object) -> None:
            """Settings a rollback-journal SQLite would make the wrong default.

            Under the default `journal_mode=delete` a writer holds the database
            exclusively, so every reader waits out the write — and Octomate writes
            constantly, from tentacles that are all reading at the same time. WAL is
            the one mode where readers and the writer proceed together; it is stored
            in the file, so this re-asserts rather than sets it. Note WAL wants a
            local filesystem and carries `-wal`/`-shm` siblings a backup must copy.

            `synchronous=NORMAL` is the setting WAL is designed for: fsync at
            checkpoints instead of every commit, which under WAL risks losing the
            last transactions to a power cut but cannot corrupt the file. The busy
            timeout is what makes concurrent writers wait their turn rather than
            fail outright on `SQLITE_BUSY`.

            `foreign_keys` is off in every SQLite connection by default, which
            parses the schema's `ON DELETE CASCADE` clauses and then ignores them.
            It is per-connection, so it belongs here rather than in a migration.
            """
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return created


@cache
def engine() -> AsyncEngine:
    return create_engine(database_settings.db_url)


@cache
def session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)


def async_session() -> AsyncSession:
    return session_maker()()
