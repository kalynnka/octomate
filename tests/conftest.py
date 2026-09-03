from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from octomate_cli.config import cli_settings
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import octomate.database as database
from octomate.config.base import OCTOMATE_HOME_ENV
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from tests.support.config import ISOLATED_HOME, without_dotenv


@pytest.fixture(scope="session", autouse=True)
def isolated_config() -> Iterator[None]:
    """No test reads the developer's config home or their environment.

    `./.octomate/` and `~/.octomate/` are both gitignored, so a suite that read
    either would assert against whatever this machine holds — green here, red on a
    bare checkout. `OCTOMATE_HOME` is obeyed without probing, which is what makes
    pointing it at `tests/config/` total for yaml: it covers `OctomateConfig`,
    `DatabaseSettings`, and anything the CLI builds several frames down.

    Three sources, three defences: the home moves the yaml, `OCTOMATE` covers the
    exported variables of both settings classes, and `without_dotenv` drops `.env`,
    which neither of the others reaches. The live suites under tests/trigger opt
    back out.
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
        patch.setenv(OCTOMATE_HOME_ENV, str(ISOLATED_HOME))
        with without_dotenv():
            yield


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes into the developer's checkout.

    Octomate keeps derived state under `.octomate/` relative to its working
    directory — a project's mirror, a thread's workspace — so a suite left in the repo
    syncs a test's throwaway project into the operator's own mirror and forks into
    their workspaces. Per test rather than per session, so two tests declaring a
    project of one name cannot land on one mirror either.

    Nothing the suite reads depends on where the working directory is: paths resolve
    from `__file__` or a `tmp_path`, and the config home comes from `OCTOMATE_HOME`
    above. The live suites under tests/trigger opt back out, since discovering this
    machine's own config home means probing the checkout.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def fresh_cli_settings() -> Iterator[None]:
    """The client config is one object per process, and a process is one command or
    one hook. This suite is the exception — it plays hundreds, each with its own
    environment, home and working directory — so the cache is dropped between them.

    Cleared after as well as before: a test that resolved the config leaves an object
    built from its own `tmp_path`, and the next one to read it must not inherit that.
    """
    cli_settings.cache_clear()
    yield
    cli_settings.cache_clear()


@pytest.fixture
async def in_memory_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[AsyncEngine]:
    """A throwaway SQLite DB with all tables created, wired into `async_session`.

    On disk under the test's own `tmp_path`, not `:memory:`. SQLAlchemy pools an
    in-memory SQLite with `StaticPool` — every session in the process shares one
    connection, because a second connection would open a second, empty database.
    Two sessions open at once then interleave each other's transactions, which is
    nothing like the `AsyncAdaptedQueuePool` production runs on and can leave a
    write rolled out from under a reader. A file gets each session its own
    connection and the WAL pragmas `create_engine` sets, so the concurrency a test
    sees is the concurrency production sees.
    """
    engine = database.create_engine(f"sqlite+aiosqlite:///{tmp_path}/octomate-test.db")
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
