from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import StaticPool

from octomate.config.base import OCTOMATE_HOME_ENV
from octomate.config.database import DEFAULT_DB_URL, DatabaseSettings
from octomate.database import async_session


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
