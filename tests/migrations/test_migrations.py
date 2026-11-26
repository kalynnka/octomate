"""The one migration check: this tree's migrations extend main's without a conflict,
and a database at main's head upgrades to this tree's head the way a deployment
upgrades one."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import to_tuple
from octomate_cli import deployment

from octomate.config.database import database_settings

REPOSITORY = Path(__file__).resolve().parents[2]
VERSIONS = "octomate/migrations/versions/"


@pytest.mark.filterwarnings(
    "error:Cannot correctly sort tables:sqlalchemy.exc.SAWarning"
)
def test_migrations_upgrade_a_database_at_main_to_this_head(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(str(deployment.ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"migrations have several heads {heads}; re-parent onto one"
    head = heads[0]

    base = request.config.getoption("--migration-base")
    assert isinstance(base, str)
    listing = subprocess.run(
        ["git", "-C", str(REPOSITORY), "ls-tree", "--name-only", base, VERSIONS],
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 0, listing.stderr
    by_file = {
        Path(revision.path).name: revision for revision in script.walk_revisions()
    }
    main_files = {
        Path(path).name for path in listing.stdout.split() if path.endswith(".py")
    }
    missing = main_files - by_file.keys()
    assert not missing, (
        f"{base}'s migrations {sorted(missing)} are not in this tree; rebase"
    )
    main = {by_file[name] for name in main_files}
    parents = {
        parent
        for revision in main
        for parent in to_tuple(revision.down_revision, default=())
    }
    main_heads = [
        revision.revision for revision in main if revision.revision not in parents
    ]
    assert len(main_heads) == 1, f"{base} has several migration heads {main_heads}"
    main_head = main_heads[0]

    database = tmp_path / "production.sqlite3"
    url = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setenv("OCTOMATE_DB_URL", url)
    monkeypatch.setattr(database_settings, "db_url", url)
    # In-process Alembic commands must not disable pytest's application loggers.
    monkeypatch.setattr("logging.config.fileConfig", Mock())
    command.upgrade(config, main_head)

    snapshot = deployment.backup_database(database)
    assert snapshot.backup is not None
    deployment.migrate(snapshot)

    assert deployment.revisions(database) == (head,)
    assert deployment.revisions(snapshot.backup) == (main_head,)
    command.check(config)
