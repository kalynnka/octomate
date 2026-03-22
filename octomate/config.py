from __future__ import annotations

import warnings
from typing import Annotated, Any, Literal, Union

from mem0.configs.base import MemoryConfig as Mem0MemoryConfig
from pydantic import BaseModel, Discriminator, Field, HttpUrl, SecretStr, Tag
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus
from octomate.tentacles.lark import LarkTentacle
from octomate.tentacles.napcat import NapcatTentacle


class TentacleConfig(BaseModel):
    name: str
    tentacle_id: str = ""

    def model_post_init(self, _context: Any) -> None:
        if not self.tentacle_id:
            self.tentacle_id = self.name


class NapcatTentacleConfig(TentacleConfig):
    type: Literal["napcat"] = "napcat"
    name: str = "napcat"
    ws_url: str
    http_url: HttpUrl
    access_token: SecretStr | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0


class LarkTentacleConfig(TentacleConfig):
    type: Literal["lark"] = "lark"
    name: str = "lark"
    app_id: str
    app_secret: SecretStr


TentacleConfigUnion = Annotated[
    Union[
        Annotated[NapcatTentacleConfig, Tag("napcat")],
        Annotated[LarkTentacleConfig, Tag("lark")],
    ],
    Discriminator("type"),
]


class Mem0Config(Mem0MemoryConfig):
    enabled: bool = Field(default=False, exclude=True)


class ZepConfig(BaseModel):
    enabled: bool = False
    api_key: str = ""


class MemoryConfig(BaseModel):
    max_messages: int = 32
    history_size: int = 16
    mem0: Mem0Config = Mem0Config()
    zep: ZepConfig = ZepConfig()


class MindConfig(BaseModel):
    model: str = "gemini-3-flash-preview"
    api_key: str = ""
    base_url: str = ""
    flush_delay: float = 0.5
    memory: MemoryConfig = MemoryConfig()


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        yaml_config_section="octomate",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
    )

    tentacles: list[TentacleConfigUnion] = []
    mind: MindConfig = MindConfig()

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )

    def build_memory(self):
        mem = self.mind.memory
        if mem.mem0.enabled:
            return Mem0Memory(
                max_messages=mem.max_messages,
                history_size=mem.history_size,
                config=mem.mem0,
            )
        if mem.zep.enabled:
            return ZepMemory(
                api_key=mem.zep.api_key,
                max_messages=mem.max_messages,
                history_size=mem.history_size,
            )
        return OctopusMemory(
            max_messages=mem.max_messages,
            history_size=mem.history_size,
        )

    def connect_tentacles(self, octopus: Octopus) -> None:
        for tc in self.tentacles:
            if isinstance(tc, NapcatTentacleConfig):
                octopus.connect(
                    NapcatTentacle(
                        tc.name,
                        octopus,
                        ws_url=tc.ws_url,
                        http_url=str(tc.http_url),
                        access_token=tc.access_token,
                        backoff_base=tc.backoff_base,
                        backoff_max=tc.backoff_max,
                        backoff_factor=tc.backoff_factor,
                        flush_delay=self.mind.flush_delay,
                    )
                )
            elif isinstance(tc, LarkTentacleConfig):
                warnings.filterwarnings(
                    "ignore", category=DeprecationWarning, module=r"lark_oapi"
                )
                octopus.connect(
                    LarkTentacle(
                        tc.name,
                        octopus,
                        app_id=tc.app_id,
                        app_secret=tc.app_secret,
                        store=octopus.store,
                        flush_delay=self.mind.flush_delay,
                    )
                )
