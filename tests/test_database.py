from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, literal, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import StaticPool
from uuid_utils.compat import uuid7

from octomate.config.base import OCTOMATE_HOME_ENV
from octomate.config.database import DEFAULT_DB_URL, DatabaseSettings
from octomate.database import async_session
from octomate.models import Base
from octomate.models.base import UTCDateTime
from octomate.schemas.spills import ToolOutputSpill


def test_every_datetime_column_uses_the_aware_mapping() -> None:
    for table in Base.metadata.tables.values():
        for column in table.c:
            assert not isinstance(column.type, DateTime), str(column)
            if isinstance(column.type, UTCDateTime):
                assert column.type.impl.timezone


@pytest.mark.parametrize(
    "offset", [timedelta(hours=5, minutes=30), timedelta(hours=-4)]
)
async def test_datetime_round_trip_preserves_the_instant_in_utc(
    in_memory_engine: AsyncEngine, offset: timedelta
) -> None:
    instant = datetime(2026, 9, 7, 1, 2, 3, tzinfo=timezone(offset))
    spill = ToolOutputSpill(handle="offset", payload=b"fixture", created_at=instant)
    async with async_session() as session:
        session.add(spill)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            ToolOutputSpill,
            expressions=[ToolOutputSpill["created_at"] == instant],
        )
    assert stored is not None
    assert stored.created_at == instant
    assert stored.created_at.tzinfo is UTC
    assert stored.model_dump(mode="json")["created_at"] == (
        instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


async def test_naive_datetimes_are_rejected_by_schema_and_orm(
    in_memory_engine: AsyncEngine,
) -> None:
    naive = datetime(2026, 9, 7, 1, 2, 3)
    with pytest.raises(ValidationError, match="timezone"):
        ToolOutputSpill(handle="naive", payload=b"fixture", created_at=naive)
    async with in_memory_engine.connect() as connection:
        with pytest.raises(StatementError, match="Datetime must include a timezone"):
            await connection.execute(select(literal(naive, type_=UTCDateTime())))


async def test_legacy_sqlite_datetimes_are_read_as_utc(
    in_memory_engine: AsyncEngine,
) -> None:
    identifier = uuid7()
    async with in_memory_engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO tool_output_spills (id, handle, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (identifier.hex, "legacy", b"fixture", "2026-09-07 01:02:03.000000"),
        )
    async with async_session() as session:
        stored = await session.get(ToolOutputSpill, identifier)
    assert stored is not None
    assert stored.created_at == datetime(2026, 9, 7, 1, 2, 3, tzinfo=UTC)


def test_db_url_defaults_when_config_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OCTOMATE_HOME_ENV, str(tmp_path))
    monkeypatch.delenv("OCTOMATE_DB_URL", raising=False)
    assert DatabaseSettings().db_url == DEFAULT_DB_URL


def test_db_url_env_beats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "octomate.yaml").write_text(
        "db_url: sqlite+aiosqlite:///from-yaml.db\n"
    )
    monkeypatch.setenv(OCTOMATE_HOME_ENV, str(tmp_path))
    monkeypatch.setenv("OCTOMATE_DB_URL", "sqlite+aiosqlite:///from-env.db")
    assert DatabaseSettings().db_url == "sqlite+aiosqlite:///from-env.db"

    monkeypatch.delenv("OCTOMATE_DB_URL")
    assert DatabaseSettings().db_url == "sqlite+aiosqlite:///from-yaml.db"


async def test_concurrent_sessions_get_their_own_connections(
    in_memory_engine: AsyncEngine,
) -> None:
    """Every concurrency test in the suite rests on this.

    An in-memory SQLite is pooled with `StaticPool` — a second connection would
    open a second, empty database, so the whole process shares one. Four
    coroutines then queue on it and no race can be reproduced, which means a test
    written against that pool passes whether or not the code beneath it is
    correct. Production pools a file URL with `AsyncAdaptedQueuePool`; the fixture
    has to do the same or it is testing a machine nobody runs.
    """
    assert not isinstance(in_memory_engine.pool, StaticPool)

    connections: list[int] = []

    async def touch() -> None:
        async with async_session() as session:
            bound = (await session.connection()).sync_connection
            assert bound is not None
            connections.append(id(bound.connection.dbapi_connection))
            # Held open, so the four overlap rather than recycling one connection.
            await anyio.sleep(0.05)

    async with anyio.create_task_group() as tasks:
        for _ in range(4):
            tasks.start_soon(touch)

    assert len(set(connections)) == 4
