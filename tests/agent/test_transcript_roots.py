"""Where each agent's transcripts are looked for by default.

The defaults are read off documented behaviour (`<CLAUDE_CONFIG_DIR or ~/.claude>/
projects`, `<CODEX_HOME or ~/.codex>/sessions`), and the ingest gates a hook's
`transcript_path` against them — so a default that is subtly wrong does not fail loudly,
it silently stops ingesting. Hence these, and hence `transcript_root` in config for when
the default is wrong anyway.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest


def projects_dirs(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> tuple[Path, ...]:
    """`CLAUDE_PROJECTS_DIRS` as it resolves for a given CLAUDE_CONFIG_DIR. Re-imported
    because it is read once at import, which is what the running app does too."""
    if value is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    module = importlib.reload(
        importlib.import_module("octomate.tentacles.agents.claude.transcript")
    )
    return module.CLAUDE_PROJECTS_DIRS


def test_the_default_is_the_documented_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert projects_dirs(monkeypatch, None) == (Path.home() / ".claude" / "projects",)


def test_claude_config_dir_relocates_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    assert projects_dirs(monkeypatch, "/tmp/elsewhere") == (
        Path("/tmp/elsewhere/projects"),
    )


def test_a_comma_separated_claude_config_dir_yields_every_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It may name several directories. Read as one path, `Path("a,b")/"projects"` is a
    root that exists nowhere — every real transcript would be refused, and the only
    sign would be a warning per turn."""
    assert projects_dirs(monkeypatch, "/tmp/a, /tmp/b") == (
        Path("/tmp/a/projects"),
        Path("/tmp/b/projects"),
    )


def test_a_configured_root_expands_home() -> None:
    """`~/...` is the natural way to write it in yaml, and pydantic keeps it literal.
    A literal `~` root matches no transcript, so the gate would refuse every one of
    them and say only that the path was outside."""
    from octomate.config import ClaudeCodeConfig, CodexConfig

    claude = ClaudeCodeConfig.model_validate(
        {"models": ["opus"], "transcript_root": "~/elsewhere/projects"}
    )
    codex = CodexConfig.model_validate({"transcript_root": "~/elsewhere/sessions"})

    assert claude.transcript_root == Path.home() / "elsewhere" / "projects"
    assert codex.transcript_root == Path.home() / "elsewhere" / "sessions"


def test_codex_home_relocates_the_sessions_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    module = importlib.reload(
        importlib.import_module("octomate.tentacles.agents.codex.transcript")
    )
    assert module.CODEX_SESSIONS_DIRS == (Path("/tmp/codex-home/sessions"),)


@pytest.fixture(autouse=True)
def restore_modules() -> Iterator[None]:
    """Reload both modules at their real env once the reloading above is done, so a
    mutated constant cannot leak into another test."""
    yield
    for name in (
        "octomate.tentacles.agents.claude.transcript",
        "octomate.tentacles.agents.codex.transcript",
    ):
        importlib.reload(importlib.import_module(name))
