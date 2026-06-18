from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from octomate.config.models import ModelConfig


class InklingConfig(BaseModel):
    model: ModelConfig = ModelConfig(
        provider="vertex",
        name="gemini-3-flash-preview",
        settings={"thinking": "medium"},
    )


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
    block is supplied. `model` is a Claude CLI model string (not a `ModelConfig`,
    since the SDK builds the model, not the provider registry). `transport`
    selects where `claude` runs — a local subprocess, or a remote host over SSH
    (which requires an `ssh` block).
    """

    cwd: str = "."
    model: str | None = None
    max_turns: int | None = None
    description: str = (
        "Claude Code — coding, file editing, shell commands, multi-step planning"
    )
    transport: Literal["local", "ssh"] = "local"
    ssh: ClaudeSSHConfig | None = None

    @model_validator(mode="after")
    def _require_ssh_block_for_ssh_transport(self) -> Self:
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError(
                "claude.transport='ssh' requires an `ssh` (ClaudeSSHConfig) block"
            )
        return self


class AgentsConfig(BaseModel):
    # Triage runs a fast/cheap inkling;
    # reception runs a stronger inkling (or claude / codex tentacle)
    triage: InklingConfig = Field(
        default_factory=lambda: InklingConfig(
            model=ModelConfig(provider="deepseek", name="deepseek-v4-flash")
        )
    )
    reception: InklingConfig = Field(
        default_factory=lambda: InklingConfig(
            model=ModelConfig(provider="deepseek", name="deepseek-v4-pro")
        )
    )
    claude: ClaudeCodeConfig | None = None
