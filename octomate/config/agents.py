from __future__ import annotations

from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, Field, model_validator

from octomate.config.models import ModelConfig

ClaudeCodeModelAlias: TypeAlias = Literal[
    "best",
    "fable",
    "sonnet",
    "opus",
    "haiku",
    "sonnet[1m]",
    "opus[1m]",
    "opusplan",
    "opusplan[1m]",
]


class InklingConfig(BaseModel):
    models: list[ModelConfig] = Field(min_length=1)

    @property
    def default_model(self) -> ModelConfig:
        return self.models[0]


class ClaudeSSHConfig(BaseModel):
    """Remote-host settings for `transport='ssh'`.

    The tentacle spawns `claude` on `host` (via the system `ssh` binary) instead
    of a local subprocess. Wired in a later phase; the shape is declared here so
    config stays stable.
    """

    host: str
    identity_file: str | None = None
    ssh_options: list[str] = Field(default_factory=list)
    claude_bin: str = "claude"


class ClaudeCodeConfig(BaseModel):
    """Claude Agent SDK runner, registered as the `claude` agent tentacle.

    Opt-in: `agents.claude` is null by default, so the agent is absent unless a
    block is supplied. `models` maps route model names to Claude CLI model
    strings (not `ModelConfig`s, since the SDK builds the model, not the
    provider registry). `transport` selects where `claude` runs — a local
    subprocess, or a remote host over SSH (which requires an `ssh` block).
    """

    cwd: str = "."
    models: set[ClaudeCodeModelAlias] = Field(
        default={"opusplan[1m]", "opus[1m]", "sonnet[1m]", "haiku"},
        min_length=1,
    )
    max_turns: int | None = None
    transport: Literal["local", "ssh"] = "local"
    ssh: ClaudeSSHConfig | None = None
    # Seconds to wait for a human approval/answer before the card expires and the
    # pending tool is denied (so the live run unblocks). None waits indefinitely.
    approval_timeout: float | None = None

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError(
                "claude.transport='ssh' requires an `ssh` (ClaudeSSHConfig) block"
            )
        return self


class AgentsConfig(BaseModel):
    inkling: InklingConfig = Field(
        default_factory=lambda: InklingConfig(
            models=[
                ModelConfig(provider="deepseek", name="deepseek-v4-flash"),
                ModelConfig(provider="deepseek", name="deepseek-v4-pro"),
            ]
        )
    )
    claude: ClaudeCodeConfig | None = None
