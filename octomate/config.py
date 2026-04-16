from __future__ import annotations

from pathlib import Path
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


class ModelConfig(BaseModel):
    model: str = "gemini-3-flash-preview"
    api_key: str = ""
    base_url: str = ""
    vertexai: bool = False
    project: str = ""
    location: str = ""
    thinking: bool | Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"


class SubAgentConfig(ModelConfig):
    description: str = ""
    system_prompt: str | None = None
    code_mode: bool = False


def _default_subagents() -> dict[str, SubAgentConfig]:
    return {
        "code": SubAgentConfig(
            description="Code execution and data analysis — uses code mode for efficient multi-tool calls",
            code_mode=True,
        )
    }


class PulseAgentConfig(BaseModel):
    type: Literal["pulse"] = "pulse"
    tag: str = "pulse"
    description: str = "Pulse — triage, planning, and step execution"
    main: ModelConfig = Field(default_factory=ModelConfig)
    subagents: dict[str, SubAgentConfig] = Field(default_factory=_default_subagents)
    skill_roots: list[Path] = Field(
        default_factory=lambda: [Path("octoskills"), Path(".octomate/skills")]
    )


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
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class LarkTentacleConfig(TentacleConfig):
    type: Literal["lark"] = "lark"
    name: str = "lark"
    app_id: str
    app_secret: SecretStr
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class SlackTentacleConfig(TentacleConfig):
    type: Literal["slack"] = "slack"
    name: str = "slack"
    bot_token: SecretStr
    app_token: SecretStr
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


TentacleConfigUnion = Annotated[
    Union[
        Annotated[NapcatTentacleConfig, Tag("napcat")],
        Annotated[LarkTentacleConfig, Tag("lark")],
        Annotated[SlackTentacleConfig, Tag("slack")],
    ],
    Discriminator("type"),
]


class SSHConfig(BaseModel):
    host: str  # user@remote-host
    identity_file: str | None = None
    ssh_options: list[str] = []
    claude_bin: str = (
        "claude"  # path to claude CLI on remote, e.g. ~/.claude/local/claude
    )


class ClaudeCodeConfig(BaseModel):
    type: Literal["claude_code"] = "claude_code"
    tag: str = "claude"
    description: str = "Claude Code - coding, file editing, shell commands, planning"
    cwd: str = "."
    model: str | None = None
    max_turns: int | None = None
    worktrees_dir: str | None = (
        None  # if set, enables per-session git worktree isolation
    )
    ssh: SSHConfig | None = None  # if set, runs Claude Code on the remote host via SSH


class CopilotConfig(BaseModel):
    type: Literal["copilot"] = "copilot"
    tag: str = "copilot"
    description: str = (
        "GitHub Copilot - remote coding agent that creates PRs from issues"
    )
    owner: str
    repo: str
    github_token: SecretStr
    base_branch: str = "main"
    model: str = ""
    custom_agent: str = ""
    custom_instructions: str = ""


class FastResearchConfig(ModelConfig):
    type: Literal["fast_research"] = "fast_research"
    tag: str = "fast_research"
    description: str = (
        "Fast Research - quick web-grounded answers with source citations"
    )


class DeepResearchConfig(BaseModel):
    type: Literal["deep_research"] = "deep_research"
    tag: str = "deep_research"
    description: str = (
        "Deep Research - thorough multi-step research producing detailed cited reports"
    )
    api_key: str = ""
    vertexai: bool = False
    project: str = ""
    location: str = ""
    agent: str = "deep-research-pro-preview-12-2025"


AgentTentacleConfigUnion = Annotated[
    Union[
        Annotated[PulseAgentConfig, Tag("pulse")],
        Annotated[ClaudeCodeConfig, Tag("claude_code")],
        Annotated[CopilotConfig, Tag("copilot")],
        Annotated[FastResearchConfig, Tag("fast_research")],
        Annotated[DeepResearchConfig, Tag("deep_research")],
    ],
    Discriminator("type"),
]


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        yaml_config_section="octomate",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
    )

    tentacles: dict[str, TentacleConfigUnion] = {}
    agents: dict[str, AgentTentacleConfigUnion] = {}

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )
