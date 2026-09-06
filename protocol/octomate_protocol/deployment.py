"""The backup record exchanged between the CLI and server maintenance process."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class DatabaseBackup(BaseModel):
    database: Path = Field(
        description="Absolute database path resolved before the update."
    )
    backup: Path | None = Field(
        description="Consistent snapshot; absent for a new database."
    )
