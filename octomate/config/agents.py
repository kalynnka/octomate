from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field
from pydantic_ai.models import KnownModelName

from octomate.config.models import ModelConfig

ClaudeCodeModelName: TypeAlias = Literal[
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
AgentRouteModelName: TypeAlias = KnownModelName | ClaudeCodeModelName


class InklingConfig(BaseModel):
    models: list[ModelConfig] = Field(min_length=1)

    @property
    def default_model(self) -> ModelConfig:
        return self.models[0]


class ClaudeSSHConfig(BaseModel):
    """Remote-host settings for the Claude tentacle.

    When `ClaudeCodeConfig.ssh` is set, the tentacle spawns `claude` on `host`
    (via the system `ssh` binary) instead of a local subprocess; leaving it null
    keeps the run local.
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
    provider registry). `ssh` selects where `claude` runs — null is a local
    subprocess; a block runs it on that remote host over SSH.
    """

    cwd: str = "."
    models: set[ClaudeCodeModelName] = Field(
        default={"opusplan[1m]", "opus[1m]", "sonnet[1m]", "haiku"},
        min_length=1,
    )
    max_turns: int | None = None
    ssh: ClaudeSSHConfig | None = None
    approval_timeout: float | None = Field(
        default=None,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the pending tool is denied (so the live run unblocks). None waits "
            "indefinitely."
        ),
    )


class AgentsConfig(BaseModel):
    inkling: InklingConfig = Field(
        default_factory=lambda: InklingConfig(
            models=[
                ModelConfig(name="deepseek:deepseek-v4-flash"),
                ModelConfig(name="deepseek:deepseek-v4-pro"),
            ]
        )
    )
    claude: ClaudeCodeConfig | None = None
