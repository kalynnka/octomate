"""YAML config loader for octomate.

Loads `octomate.yaml` by default; override path via `OCTOMATE_CONFIG`
env var. Agent/channel slots are named fields — adding a Slack/Lark/etc.
tentacle adds one field on `ChannelsConfig`, no loader changes.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///.octomate/octomate.db"


class InklingConfig(BaseModel):
    model: str = "google:gemini-3-flash-preview"
    system_prompt: str | None = None


class DevUIConfig(BaseModel):
    agent_id: str = "inkling"


class AgentsConfig(BaseModel):
    inkling: InklingConfig = Field(default_factory=InklingConfig)


class ChannelsConfig(BaseModel):
    dev_ui: DevUIConfig = Field(default_factory=DevUIConfig)


class OctomateConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)


DEFAULT_CONFIG_PATH = "octomate.yaml"


def load_config(path: str | os.PathLike[str] | None = None) -> OctomateConfig:
    """Load `OctomateConfig` from YAML.

    Priority: explicit `path` arg → `OCTOMATE_CONFIG` env → `octomate.yaml`.
    Returns defaults if the resolved path doesn't exist (useful for tests
    and ad-hoc dev where the file may not be checked in).
    """
    resolved = Path(path or os.environ.get("OCTOMATE_CONFIG", DEFAULT_CONFIG_PATH))
    if not resolved.exists():
        return OctomateConfig()
    with resolved.open("r") as f:
        data = yaml.safe_load(f) or {}
    return OctomateConfig.model_validate(data)
