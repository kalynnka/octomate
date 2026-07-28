from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TypeAlias, get_args

from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import AfterValidator, BaseModel, Field
from pydantic_ai.models import KnownModelName
from pydantic_ai.settings import ThinkingEffort

from octomate.config.models import ModelConfig


# A filesystem path from config, with `~` meaning what the person writing it meant:
# pydantic keeps `~/...` literal, and `Path("~/x").resolve()` yields `<cwd>/~/x` rather
# than a home directory — a root like that matches nothing and quietly stops a session
# being ingested.
ConfigPath: TypeAlias = Annotated[Path, AfterValidator(Path.expanduser)]

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
# Codex takes a free-form model string in `thread_start(model=...)`; these are the
# route-name labels the config and channel `agents` lists select from.
CodexModelName: TypeAlias = Literal[
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.3-codex",
    "gpt-5.1-codex-mini",
]
CodexApprovalMode: TypeAlias = Literal["auto_review", "user", "deny_all"]
CodexPersonality: TypeAlias = Literal["none", "friendly", "pragmatic"]
CodexReasoningEffort: TypeAlias = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
]
CodexReasoningSummary: TypeAlias = Literal[
    "auto",
    "concise",
    "detailed",
    "none",
]
CodexSandbox: TypeAlias = Literal["read_only", "workspace_write", "full_access"]
AgentRouteModelName: TypeAlias = KnownModelName | ClaudeCodeModelName | CodexModelName


@dataclass(frozen=True)
class Claim:
    # What this route is for — per-route, not per-agent, so two models of one
    # agent can advertise differently.
    ability: str
    # The effort levels this route accepts from a caller — pydantic-ai's thinking
    # scale, the one effort vocabulary across all agents; each tentacle maps it
    # onto its runtime's knob. Defaults to the full scale.
    efforts: tuple[ThinkingEffort, ...] = get_args(ThinkingEffort)

    def __str__(self) -> str:
        return f"[effort {'/'.join(self.efforts)}] {self.ability}"


class InklingConfig(BaseModel):
    models: list[ModelConfig] = Field(min_length=1)
    claims: dict[KnownModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )

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
    claims: dict[ClaudeCodeModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )
    max_turns: int | None = None
    ssh: ClaudeSSHConfig | None = None
    transcript_root: ConfigPath | None = Field(
        default=None,
        description=(
            "An additional directory native session transcripts may live under; a hook "
            "naming a path outside every known root is not tailed. Claude's own "
            "<CLAUDE_CONFIG_DIR or ~/.claude>/projects is always accepted, so this "
            "widens the set rather than replacing it — set it when Claude Code writes "
            "somewhere else too, since the default is where it writes today rather than "
            "a promise it always will."
        ),
    )
    approval_timeout: float | None = Field(
        default=None,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the pending tool is denied (so the live run unblocks). None waits "
            "indefinitely."
        ),
    )


class CodexConfig(BaseModel):
    """OpenAI Codex SDK runner, registered as the `codex` agent tentacle.

    Opt-in: `agents.codex` is null by default, so the agent is absent unless a
    block is supplied. `models` are route-name labels passed straight to the SDK's
    `thread_start(model=...)` / `turn(..., model=...)` calls. Most fields below
    are default arguments for Codex SDK calls; the tentacle may compose them with
    per-run overrides before calling the SDK.
    """

    enabled: bool = Field(
        default=True,
        description="Whether to register the Codex tentacle when the config block exists.",
    )
    transcript_root: ConfigPath | None = Field(
        default=None,
        description=(
            "An additional directory native session rollouts may live under; a hook "
            "naming a path outside every known root is not tailed. Codex's own "
            "<CODEX_HOME or ~/.codex>/sessions is always accepted, so this widens the "
            "set rather than replacing it — set it when Codex writes somewhere else "
            "too, since the default is where it writes today rather than a promise it "
            "always will."
        ),
    )
    cwd: str = Field(
        default=".",
        description=(
            "Default working directory for Codex thread_start, thread_resume, "
            "and turn calls."
        ),
    )
    runtime: CodexSdkConfig = Field(
        default_factory=CodexSdkConfig,
        description=(
            "Default SDK runtime config used to launch the local Codex app-server."
        ),
    )
    models: set[CodexModelName] = Field(
        default={
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.3-codex",
            "gpt-5.1-codex-mini",
        },
        min_length=1,
        description="Codex model route labels this agent exposes to channels.",
    )
    claims: dict[CodexModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )
    approval_mode: CodexApprovalMode = Field(
        default="user",
        description=(
            "Default approval behavior for Codex threads and turns: auto_review "
            "uses the SDK auto reviewer, user bridges requests to Octomate "
            "approval/question cards, and deny_all never asks."
        ),
    )
    sandbox: CodexSandbox = Field(
        default="workspace_write",
        description="Default SDK filesystem sandbox preset for Codex threads and turns.",
    )
    base_instructions: str | None = Field(
        default=None,
        description="Default base instructions for new Codex threads.",
    )
    developer_instructions: str | None = Field(
        default=None,
        description=(
            "Default developer instructions for starting or resuming Codex threads."
        ),
    )
    ephemeral: bool | None = Field(
        default=None,
        description="Default ephemeral flag for newly started Codex threads.",
    )
    model_provider: str | None = Field(
        default=None,
        description="Default Codex model provider override for threads.",
    )
    personality: CodexPersonality | None = Field(
        default=None,
        description="Default Codex personality preset for threads and turns.",
    )
    effort: CodexReasoningEffort | None = Field(
        default=None,
        description="Default reasoning effort override for Codex turns.",
    )
    summary: CodexReasoningSummary | None = Field(
        default=None,
        description="Default reasoning summary setting for Codex turns.",
    )
    approval_timeout: float | None = Field(
        default=None,
        description=(
            "Seconds to wait for a human Codex approval/answer before the card "
            "expires and the SDK request is denied. None waits indefinitely."
        ),
    )
    max_clients: int | None = Field(
        default=8,
        ge=1,
        description=(
            "Max warm Codex app-server processes kept in the per-thread client "
            "pool. When exceeded, the least-recently-used idle client is closed. "
            "None keeps every thread's client until shutdown."
        ),
    )
    client_idle_ttl: float | None = Field(
        default=600.0,
        description=(
            "Seconds a pooled Codex client may sit idle before it is closed on the "
            "next pool access. None keeps idle clients until shutdown."
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
    codex: CodexConfig | None = None
