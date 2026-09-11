import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from octomate.config.database import database_settings

PREVIOUS = "f973cff9f077"
REVISION = "ebb904508e50"
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
            "SELECT mcp_id, encrypted_tokens FROM oauth_connections WHERE id = ?",
            (GRANT,),
        ).fetchone() == (None, b"unchanged encrypted grant")
        for mcp_id, namespace in (
            (MCP, "personal/work"),
            (SECOND, "personal/personal"),
        ):
            connection.execute(
                "INSERT INTO mcp (id, user_id, name, namespace, url, enabled, auth_kind, "
                "created_at, updated_at, tentacle_id) VALUES (?, ?, 'Work', ?, "
                "'https://mcp.example/mcp', 1, 'oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'github')",
                (mcp_id, USER, namespace),
            )
            connection.execute(
                "INSERT INTO oauth_connections (id, user_id, connector_id, status, encrypted_tokens, "
                "scopes, created_at, updated_at, mcp_id) VALUES (?, ?, 'github', 'active', ?, "
                "'[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
                (mcp_id, USER, b"independent grant", mcp_id),
            )
        assert connection.execute(
            "SELECT instructions FROM mcp WHERE id = ?", (MCP,)
        ).fetchone() == ("",)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO oauth_connections (id, user_id, connector_id, status, encrypted_tokens, "
                "subject, account_label, scopes, expires_at, created_at, updated_at, mcp_id) "
                "SELECT 'duplicate', user_id, connector_id, status, "
                "encrypted_tokens, subject, account_label, scopes, expires_at, created_at, updated_at, mcp_id "
                "FROM oauth_connections WHERE id = ?",
                (GRANT,),
            )
        connection.execute(
            "INSERT INTO oauth_operations (id, user_id, connector_id, mcp_id, encrypted_data, expires_at, created_at) "
            "VALUES ('pending', ?, 'github', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (USER, MCP, b"pending authorization"),
        )
        connection.execute("DELETE FROM mcp WHERE id = ?", (MCP,))
        assert set(
            connection.execute("SELECT id FROM oauth_connections").fetchall()
        ) == {
            (GRANT,),
            (SECOND,),
        }
        assert connection.execute("SELECT id FROM oauth_operations").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(target.with_name("source.db")) as original:
        assert original.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS,)
        assert original.execute(
            "SELECT encrypted_tokens FROM oauth_connections WHERE id = ?", (GRANT,)
        ).fetchone() == (b"unchanged encrypted grant",)
        assert (
            original.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp'"
            ).fetchone()
            is None
        )


def test_migration_roundtrips_existing_unscoped_grants(
    migration_copy: tuple[Config, Path],
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS,)
        assert connection.execute(
            "SELECT encrypted_tokens FROM oauth_connections WHERE id = ?", (GRANT,)
        ).fetchone() == (b"unchanged encrypted grant",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp'"
            ).fetchone()
            is None
        )
        assert "mcp_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(oauth_connections)")
        }


@pytest.mark.parametrize("pending", [False, True])
def test_downgrade_does_not_discard_mcp_authorizations(
    migration_copy: tuple[Config, Path],
    pending: bool,
) -> None:
    config, target = migration_copy
    command.upgrade(config, REVISION)
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO mcp (id, user_id, name, namespace, url, enabled, auth_kind, created_at, updated_at) "
            "VALUES (?, ?, 'Work', 'personal/work', 'https://mcp.example/mcp', 1, 'oauth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (MCP, USER),
        )
        if pending:
            connection.execute(
                "INSERT INTO oauth_operations (id, user_id, connector_id, mcp_id, encrypted_data, expires_at, created_at) "
                "VALUES ('pending', ?, 'github', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (USER, MCP, b"pending authorization"),
            )
        else:
            connection.execute(
                "UPDATE oauth_connections SET mcp_id = ? WHERE id = ?",
                (MCP, GRANT),
            )
    with pytest.raises(RuntimeError, match="cannot represent"):
        command.downgrade(config, PREVIOUS)
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (REVISION,)
        assert connection.execute(
            "SELECT encrypted_tokens FROM oauth_connections WHERE id = ?", (GRANT,)
        ).fetchone() == (b"unchanged encrypted grant",)
        if pending:
            assert connection.execute(
                "SELECT encrypted_data FROM oauth_operations WHERE id = 'pending'"
            ).fetchone() == (b"pending authorization",)
