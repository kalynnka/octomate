from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource


class TentacleConfig(BaseModel):
    name: str
    tentacle_id: str = ""

    def model_post_init(self, _context: Any) -> None:
        if not self.tentacle_id:
            self.tentacle_id = self.name


class NapcatTentacleConfig(TentacleConfig):
    ws_url: str
    access_token: str | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0


class BrainConfig(BaseModel):
    model: str = "google-gla:gemini-3-flash-preview"
    system_prompt: str = "You are a helpful assistant."
    flush_delay: float = 0.5


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_",
        env_nested_delimiter="__",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
    )

    tentacles: list[NapcatTentacleConfig] = []
    brain: BrainConfig = BrainConfig()

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )
