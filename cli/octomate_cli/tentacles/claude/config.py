"""Configuration file operations shared by the claude tentacle's installers."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError

from octomate_cli.tentacles.claude.schema import McpSettings


def load_settings(path: Path) -> McpSettings:
    if not path.exists():
        return McpSettings()
    content = path.read_bytes()
    if not content.strip():
        return McpSettings()
    try:
        return McpSettings.model_validate_json(content)
    except ValidationError as error:
        raise typer.BadParameter(f"Invalid MCP settings in {path}: {error}") from error


def write_settings(path: Path, settings: McpSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        settings.model_dump_json(indent=2, by_alias=True, exclude_unset=True) + "\n"
    )
