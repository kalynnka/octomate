from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import octomate.database as database
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from tests.support.config import ISOLATED_SOURCES, config_sources


@pytest.fixture(scope="session", autouse=True)
def isolated_config() -> Iterator[None]:
    """No test reads the developer's `octomate.yaml` or their environment.

    Both are gitignored, so a suite that merged either would assert against
    whatever this machine holds — green here, red on a bare checkout. The
    environment half is not theoretical: an editor that loads `.env` for the test
    run (VS Code does by default) supplies half a channel's secrets, and the
    config fails on the half it did not. `OCTOMATE` covers the prefixes of both
    settings classes. The live suites under tests/trigger opt back out.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in [
            name
            for name in os.environ
            # `OCTOMATE_DB_URL` is the harness's own, from pyproject's `env`. Clearing
            # it would send a freshly built `DatabaseSettings` back to the real
            # database; the tests that exercise it manage the variable themselves.
            if name.startswith("OCTOMATE") and name != "OCTOMATE_DB_URL"
        ]:
            patch.delenv(name)
        with config_sources(ISOLATED_SOURCES):
            yield


@pytest.fixture
async def in_memory_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """An in-memory SQLite DB with all tables created, wired into `async_session`."""
    engine = database.create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(
        engine,
        class_=database.AsyncSession,
        expire_on_commit=False,
    )

    database.engine.cache_clear()
    database.session_maker.cache_clear()
    monkeypatch.setattr(database, "engine", lambda: engine)
    monkeypatch.setattr(database, "session_maker", lambda: maker)

    with sqlalchemy_materia:
        yield engine

    await engine.dispose()
