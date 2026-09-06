"""Configuration file operations shared by the claude tentacle's installers."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from octomate_cli.tentacles.types import JsonObject


def load_settings(path: Path) -> JsonObject:
    if not path.exists() or not path.read_text().strip():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} is not a JSON object")
    return data


def write_settings(path: Path, settings: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
