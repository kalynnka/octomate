from __future__ import annotations

from pathlib import Path

import pytest

from octomate.config.database import DEFAULT_DB_URL, DatabaseSettings


def test_db_url_defaults_when_config_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "octomate.yaml").write_text("octomate: {}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OCTOMATE_DB_URL", raising=False)
    assert DatabaseSettings().db_url == DEFAULT_DB_URL


def test_db_url_env_beats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "octomate.yaml").write_text(
        "octomate:\n  db_url: sqlite+aiosqlite:///from-yaml.db\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OCTOMATE_DB_URL", "sqlite+aiosqlite:///from-env.db")
    assert DatabaseSettings().db_url == "sqlite+aiosqlite:///from-env.db"

    monkeypatch.delenv("OCTOMATE_DB_URL")
    assert DatabaseSettings().db_url == "sqlite+aiosqlite:///from-yaml.db"
