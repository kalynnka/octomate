"""Application and tentacle configuration via pydantic-settings."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class TentacleConfig(BaseModel):
    """Base configuration shared by all tentacle types."""

    name: str


class NapcatTentacleConfig(TentacleConfig):
    """Configuration for a napcat WebSocket tentacle."""

    ws_url: str
    access_token: str | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0


class OctomateConfig(BaseSettings):
    """Root configuration loaded from env / dotenv / CLI."""

    model_config = {"env_prefix": "OCTOMATE_", "env_nested_delimiter": "__"}

    tentacles: list[TentacleConfig] = []
