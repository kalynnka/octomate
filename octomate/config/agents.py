from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import AfterValidator, BaseModel, Field, field_validator
from pydantic_ai.models import KnownModelName
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai_harness.tool_output_limits import TruncationStrategy

from octomate.config.models import ModelConfig
from octomate.types.permissions import (
    ClaudePermissionMode,
    CodexPermissionMode,
    InklingPermissionMode,
)

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
CodexPersonality: TypeAlias = Literal["none", "friendly", "pragmatic"]
CodexSandbox: TypeAlias = Literal["read_only", "workspace_write", "full_access"]
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
AgentRouteModelName: TypeAlias = KnownModelName | ClaudeCodeModelName | CodexModelName


@dataclass(frozen=True)
class Claim:
    # What this route is for — per-route, not per-agent, so two models of one
    # agent can advertise differently.
    ability: str
    # The effort levels this route accepts from a caller — the one effort
    # vocabulary across all agents; each tentacle maps it onto its runtime's
    # knob. Defaults to the full scale, so a route whose provider takes less
    # (DeepSeek has no `minimal`) must say so.
    efforts: tuple[ThinkingEffort, ...] = ("minimal", "low", "medium", "high", "xhigh")

    def __str__(self) -> str:
        return f"[effort {'/'.join(self.efforts)}] {self.ability}"


class TruncateAction(BaseModel):
    """Clamp the return to a character budget. Lossy, and costs nothing."""

    kind: Literal["truncate"] = "truncate"
    max_chars: int = Field(
        default=4_000,
        gt=0,
        description="Characters kept. Counted in characters even when the bands "
        "measure in tokens, since truncation is a character operation.",
    )
    strategy: TruncationStrategy = Field(
        default=TruncationStrategy.head_tail,
        description="Which end survives: `head`, `tail`, or `head_tail` (both, with "
        "the middle elided). `tail` suits a command's output, where the outcome is "
        "at the end; `head` suits a long listing.",
    )


class SpillAction(BaseModel):
    """Store the return whole and hand the model a handle to it. Lossless."""

    kind: Literal["spill"] = "spill"
    preview_chars: int = Field(
        default=1_000,
        gt=0,
        description="Characters shown inline beside the handle, so the model can "
        "judge whether reading the rest back is worth a tool call.",
    )


class SummarizeAction(BaseModel):
    """Replace the return with an LLM summary. Lossy, and costs a model call.

    The summary is written by the run's own model. There is deliberately no model
    override: a name given here would be resolved by pydantic-ai rather than the
    provider registry that builds every other model this agent runs on, so the two
    would disagree about what the same name means.
    """

    kind: Literal["summarize"] = "summarize"


ToolOutputAction: TypeAlias = Annotated[
    TruncateAction | SpillAction | SummarizeAction, Field(discriminator="kind")
]


class ToolOutputBand(BaseModel):
    """A size threshold, and what a return reaching it is replaced with."""

    over: int = Field(
        gt=0,
        description="Size a return must reach for this band to claim it, in "
        "characters (or tokens, when `over_tokens` is set).",
    )
    action: ToolOutputAction


class ToolOutputConfig(BaseModel):
    """What becomes of a tool return too large to sit in the context window.

    A tool return persists in history, so an oversized one is re-sent on every later
    request for the rest of the conversation. Each return is measured once and the
    **largest** band whose `over` it reaches claims it; anything under every threshold
    is left alone, and bands may be listed in any order. A band that cannot run falls
    back to truncation, so the payload is bounded either way.
    """

    enabled: bool = Field(
        default=False,
        description="Whether oversized tool returns are reduced at all. Turning this "
        "off sends every return to the model whole, which is a debugging posture "
        "rather than a deployment one.",
    )
    bands: list[ToolOutputBand] = Field(
        default_factory=lambda: [
            ToolOutputBand(over=10_000, action=SpillAction()),
            ToolOutputBand(over=100_000, action=SummarizeAction()),
        ],
        min_length=1,
        description="Size thresholds and their actions. The default spills past ~10k, "
        "keeping the payload readable through `read_tool_result`, and summarizes past "
        "~100k, where a handle buys the model little — reading a payload that size "
        "back costs more calls than the summary it would have to reconstruct. To "
        "reduce nothing, set `enabled: false` rather than emptying this.",
    )
    over_tokens: bool = Field(
        default=False,
        description="Measure band thresholds in estimated tokens instead of "
        "characters, via a ~4-chars-per-token heuristic.",
    )
    retention_hours: float | None = Field(
        default=6.0,
        gt=0,
        description="How long a spilled payload stays readable before it is pruned. "
        "A spill outlives its run on purpose, since the handle may be read back turns "
        "later, but this host is long-lived and the table would only grow. Null keeps "
        "payloads forever.",
    )

    @property
    def retention(self) -> timedelta | None:
        return (
            timedelta(hours=self.retention_hours)
            if self.retention_hours is not None
            else None
        )


class InklingConfig(BaseModel):
    models: list[ModelConfig] = Field(min_length=1)

    request_limit: int = Field(
        default=256,
        gt=0,
        description="Maximum model requests in one Inkling run.",
    )

    claims: dict[KnownModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )
    tool_output: ToolOutputConfig = Field(
        default_factory=ToolOutputConfig,
        description="How oversized tool returns are cut down before they reach — and "
        "stay in — the model's context.",
    )
    permission_mode: InklingPermissionMode = Field(
        default="default",
        description=(
            "Approval posture for an Inkling conversation whose thread is in no "
            "project, or whose project declares none. Claude's scale, narrowed to "
            "what deferred approvals can resolve."
        ),
    )

    @property
    def default_model(self) -> ModelConfig:
        return self.models[0]


class ClaudeSSHConfig(BaseModel):
    """Remote-host settings for the Claude tentacle.

    When `ClaudeCodeConfig.ssh` is set, the tentacle spawns `claude` on `host`
    (via the system `ssh` binary) instead of a local subprocess; leaving it null
    keeps the run local. Setting it is currently refused — see `ClaudeCodeConfig.ssh`.
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
    subprocess; a block would run it on that remote host over SSH, and is
    refused while remote runs are disabled.
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
    permission_mode: ClaudePermissionMode = Field(
        default="default",
        description=(
            "Approval posture for a Claude conversation whose thread is in no "
            "project, or whose project declares none. Handed to the SDK verbatim."
        ),
    )
    max_turns: int | None = None
    ssh: ClaudeSSHConfig | None = Field(
        default=None,
        description=(
            "Remote host to run `claude` on. Disabled: a run belongs to the project "
            "its thread is in, and a project root is a local path the host on the "
            "other end has nothing to match, so a remote run would silently land "
            "somewhere else. `SSHTransport` is kept as it is; `refuse_remote_runs` "
            "is what to drop when a remote run can say where it is."
        ),
    )
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

    @field_validator("ssh")
    @classmethod
    def refuse_remote_runs(cls, ssh: ClaudeSSHConfig | None) -> ClaudeSSHConfig | None:
        if ssh is not None:
            raise ValueError(
                "remote runs are disabled: a run happens in its thread's project "
                "root, which is a local path the remote host has nothing to match"
            )
        return ssh


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
    permission_mode: CodexPermissionMode = Field(
        default="user_review",
        description=(
            "Approval posture a Codex conversation falls back to when it carries none "
            "of its own: who answers when the agent asks to step past the sandbox — "
            "the user, the SDK's reviewer, or nobody. `CODEX_PERMISSION_PLANS` maps "
            "each onto the SDK's approval policy and reviewer."
        ),
    )
    sandbox: CodexSandbox = Field(
        default="workspace_write",
        description=(
            "SDK filesystem sandbox preset for Codex threads and turns: what a command "
            "may touch when nobody is asked. The operator's, and fixed for a run — "
            "deliberately not folded into `permission_mode`, so a conversation's "
            "approval posture never rewrites what the whole thread reaches."
        ),
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
