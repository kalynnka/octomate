from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from mem0.configs.base import MemoryConfig as Mem0MemoryConfig
from pydantic import BaseModel, Discriminator, Field, HttpUrl, SecretStr, Tag
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource


class TentacleConfig(BaseModel):
    name: str
    tentacle_id: str = ""
    flush_delay: float = 0.5

    def model_post_init(self, _context: Any) -> None:
        if not self.tentacle_id:
            self.tentacle_id = self.name


class FlickConfig(BaseModel):
    enabled: bool = False
    model: str = "gemini-3-flash-preview"
    api_key: str = ""
    base_url: str = ""


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


class NapcatTentacleConfig(TentacleConfig):
    type: Literal["napcat"] = "napcat"
    name: str = "napcat"
    ws_url: str
    http_url: HttpUrl
    access_token: SecretStr | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0
    flick: FlickConfig = Field(default_factory=FlickConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class LarkTentacleConfig(TentacleConfig):
    type: Literal["lark"] = "lark"
    name: str = "lark"
    app_id: str
    app_secret: SecretStr
    flick: FlickConfig = Field(default_factory=FlickConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class SlackTentacleConfig(TentacleConfig):
    type: Literal["slack"] = "slack"
    name: str = "slack"
    bot_token: SecretStr
    app_token: SecretStr
    flick: FlickConfig = Field(default_factory=FlickConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


TentacleConfigUnion = Annotated[
    Union[
        Annotated[NapcatTentacleConfig, Tag("napcat")],
        Annotated[LarkTentacleConfig, Tag("lark")],
        Annotated[SlackTentacleConfig, Tag("slack")],
    ],
    Discriminator("type"),
]


class ClaudeCodeConfig(BaseModel):
    type: Literal["claude_code"] = "claude_code"
    tag: str = "claude"
    description: str = "Claude Code - coding, file editing, shell commands, planning"
    cwd: str = "."
    model: str | None = None
    max_turns: int | None = None
    worktrees_dir: str | None = None  # if set, enables per-session git worktree isolation


AgentTentacleConfigUnion = ClaudeCodeConfig


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        yaml_config_section="octomate",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
    )

    tentacles: list[TentacleConfigUnion] = []
    agents: list[AgentTentacleConfigUnion] = []

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )
