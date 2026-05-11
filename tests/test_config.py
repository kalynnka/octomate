"""Unit tests for YAML config loading + nested-object dispatch."""

from __future__ import annotations

from pathlib import Path

from octomate.config import (
    DevUIConfig,
    InklingConfig,
    OctomateConfig,
    load_config,
)


def test_load_config_defaults_when_path_missing(tmp_path: Path) -> None:
    """Missing file → OctomateConfig() defaults; doesn't raise."""
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert isinstance(cfg, OctomateConfig)
    assert isinstance(cfg.agents.inkling, InklingConfig)
    assert isinstance(cfg.channels.dev_ui, DevUIConfig)


def test_load_config_parses_yaml(tmp_path: Path) -> None:
    yaml = tmp_path / "octomate.yaml"
    yaml.write_text(
        """
database:
  url: "sqlite+aiosqlite:///:memory:"
agents:
  inkling:
    model: google:gemini-3-flash-preview
channels:
  dev_ui:
    agent_id: inkling
"""
    )
    cfg = load_config(yaml)
    assert cfg.database.url == "sqlite+aiosqlite:///:memory:"
    assert cfg.agents.inkling.model == "google:gemini-3-flash-preview"
    assert cfg.channels.dev_ui.agent_id == "inkling"


def test_load_config_defaults_from_repo_yaml() -> None:
    """The committed octomate.yaml at repo root must parse cleanly."""
    cfg = load_config(Path(__file__).parent.parent / "octomate.yaml")
    assert isinstance(cfg.agents.inkling, InklingConfig)
    assert isinstance(cfg.channels.dev_ui, DevUIConfig)
