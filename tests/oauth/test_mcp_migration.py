import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from octomate.config.database import database_settings

PREVIOUS = "a9f67a554bb6"
REVISION = "515e2ff21675"
USER = "10000000000000000000000000000001"
MCP = "20000000000000000000000000000001"
GRANT = "30000000000000000000000000000001"
SECOND = "20000000000000000000000000000002"


@pytest.fixture
def migration_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, Path]:
    source = tmp_path / "source.db"
    target = tmp_path / "migration-copy.db"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "octomate" / "migrations"),
    )
    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{source}")
    assert Path(make_url(database_settings.db_url).database or "").resolve() == source
    command.upgrade(config, PREVIOUS)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (id, username, name) VALUES (?, 'alice', 'Alice')",
            (USER,),
        )
        connection.execute(
            "INSERT INTO oauth_connections (id, user_id, connector_id, status, encrypted_tokens, "
            "subject, account_label, scopes, created_at, updated_at) "
            "VALUES (?, ?, 'github', 'active', ?, 'account', 'Account', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GRANT, USER, b"unchanged encrypted grant"),
        )
        connection.execute(
            "INSERT INTO mcp (id, user_id, name, namespace, url, enabled, auth_kind, "
            "created_at, updated_at, oauth_connection_id) VALUES (?, ?, 'Work', 'personal/work', "
            "'https://mcp.example/mcp', 1, 'oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (MCP, USER, GRANT),
        )
    with sqlite3.connect(source) as original, sqlite3.connect(target) as copied:
        original.backup(copied)
    monkeypatch.setattr(database_settings, "db_url", f"sqlite+aiosqlite:///{target}")
    assert source != target
    assert Path(make_url(database_settings.db_url).database or "").resolve() == target
    return config, target


def test_migration_preserves_grants_and_allows_independent_connections(
    migration_copy: tuple[Config, Path],
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT tentacle_id FROM mcp WHERE id = ?", (MCP,)
        ).fetchone() == ("github",)
        assert connection.execute(
            "SELECT mcp_id, encrypted_tokens FROM oauth_connections WHERE id = ?",
            (GRANT,),
        ).fetchone() == (MCP, b"unchanged encrypted grant")
        connection.execute(
            "INSERT INTO mcp (id, user_id, name, namespace, url, enabled, auth_kind, "
            "created_at, updated_at, tentacle_id) VALUES (?, ?, 'Personal', 'personal/personal', "
            "'https://mcp.example/mcp', 1, 'oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'github')",
            (SECOND, USER),
        )
        connection.execute(
            "INSERT INTO oauth_connections (id, user_id, connector_id, status, encrypted_tokens, "
            "scopes, created_at, updated_at, mcp_id) VALUES ('second-grant', ?, 'github', 'active', ?, "
            "'[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (USER, b"second grant", SECOND),
        )
        connection.execute(
            "INSERT INTO oauth_operations (id, user_id, connector_id, mcp_id, encrypted_data, expires_at, created_at) "
            "VALUES ('pending', ?, 'github', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (USER, MCP, b"pending authorization"),
        )
        connection.execute("DELETE FROM mcp WHERE id = ?", (MCP,))
        assert connection.execute("SELECT id FROM oauth_connections").fetchall() == [
            ("second-grant",)
        ]
        assert connection.execute("SELECT id FROM oauth_operations").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(target.with_name("source.db")) as original:
        assert original.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS,)
        assert original.execute(
            "SELECT oauth_connection_id FROM mcp WHERE id = ?", (MCP,)
        ).fetchone() == (GRANT,)


def test_migration_refuses_ambiguous_old_grants_before_changing_schema(
    migration_copy: tuple[Config, Path],
) -> None:
    config, target = migration_copy
    with sqlite3.connect(target) as connection:
        connection.execute(
            "INSERT INTO mcp (id, user_id, name, namespace, url, enabled, auth_kind, created_at, updated_at, oauth_connection_id) "
            "SELECT ?, user_id, name, 'personal/duplicate', url, enabled, auth_kind, created_at, updated_at, oauth_connection_id FROM mcp WHERE id = ?",
            (SECOND, MCP),
        )
    with pytest.raises(RuntimeError, match="shared OAuth grant"):
        command.upgrade(config, REVISION)
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS,)
        assert "tentacle_id" not in [
            row[1] for row in connection.execute("PRAGMA table_info(mcp)")
        ]


def test_downgrade_does_not_discard_mcp_grants(
    migration_copy: tuple[Config, Path],
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    with pytest.raises(RuntimeError, match="cannot represent"):
        command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (REVISION,)
        assert connection.execute(
            "SELECT encrypted_tokens FROM oauth_connections WHERE id = ?", (GRANT,)
        ).fetchone() == (b"unchanged encrypted grant",)
