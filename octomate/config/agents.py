from __future__ import annotations

import logging
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
    DeepseekPermissionMode,
    InklingPermissionMode,
)

# A filesystem path from config, with `~` meaning what the person writing it meant:
# pydantic keeps `~/...` literal, and `Path("~/x").resolve()` yields `<cwd>/~/x` rather
# than a home directory — a root like that matches nothing and quietly stops a session
# being ingested.
ConfigPath: TypeAlias = Annotated[Path, AfterValidator(Path.expanduser)]

logger = logging.getLogger(__name__)

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
# dsh identifies a model by (provider, model id); these are the model-id labels the
# routes select from, all under `DeepseekConfig.provider`.
DeepseekModelName: TypeAlias = Literal["deepseek-v4-flash", "deepseek-v4-pro"]
AgentRouteModelName: TypeAlias = (
    KnownModelName | ClaudeCodeModelName | CodexModelName | DeepseekModelName
)


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
        default=True,
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


class AgentConfig(BaseModel):
    """What every agent tentacle's config block declares, whichever runtime it
    drives: the agent reads the same way everywhere — declared and enabled, or
    absent — and carries its own half of the gateway switch."""

    enabled: bool = Field(
        default=True,
        description="Whether to register the tentacle when its config block exists.",
    )
    gateway: bool = Field(
        default=True,
        description="Whether this agent's driven turns offer the gateway spells. Off, "
        "no channel connection can switch them on for it.",
    )


class InklingConfig(AgentConfig):
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


class ClaudeCodeConfig(AgentConfig):
    """Claude Agent SDK runner, registered as the `claude` agent tentacle.

    Opt-in: `agents.claude` is null by default, so the agent is absent unless a
    block is supplied. `models` maps route model names to Claude CLI model
    strings (not `ModelConfig`s, since the SDK builds the model, not the
    provider registry). `ssh` selects where `claude` runs — null is a local
    subprocess; a block would run it on that remote host over SSH, and is
    refused while remote runs are disabled.
    """

    enabled: bool = Field(
        default=True,
        description="Whether to register the Claude tentacle when the config block exists.",
    )
    models: set[ClaudeCodeModelName] = Field(
        min_length=1,
        description="Claude Code model route labels this agent exposes to channels.",
    )
    claims: dict[ClaudeCodeModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )
    native_gateway: bool = Field(
        default=True,
        description="Whether anonymous claude-native sessions may cast the served "
        "gateway spells over `/gateway/mcp`. The bearer still authenticates every "
        "call; this is the runtime's own switch, matched against the "
        "X-Octomate-Client header a static install writes.",
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
            "Remote host to run `claude` on. Disabled, and no longer wired: a "
            "run happens in its thread's workspace, and there is nothing that "
            "makes a workspace on another machine. The tentacle hands the SDK "
            "no transport at all now; `SSHTransport` is kept as it stands, but "
            "nothing constructs it. Re-enabling is three things rather than "
            "one — somewhere remote to fork a workspace into, a directory on "
            "`ClaudeSSHConfig` to name it, and the transport wired back in. "
            "Setting it is warned about rather than refused: with the transport "
            "parked the block reaches nothing, so failing a start over it would "
            "cost more than it saves."
        ),
    )
    approval_timeout: float | None = Field(
        default=3600.0,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the pending tool is denied (so the live run unblocks). An hour by "
            "default, because not answering is the ordinary case rather than the "
            "exotic one, and an unbounded wait leaves the thread unusable for good. "
            "None waits indefinitely."
        ),
    )

    @field_validator("ssh")
    @classmethod
    def warn_remote_runs_are_off(
        cls, ssh: ClaudeSSHConfig | None
    ) -> ClaudeSSHConfig | None:
        """Say that a configured remote host is not honoured, and keep it as written.

        This refused the whole config while the tentacle still built an SSH
        transport, since a block that would have been obeyed had to be stopped
        loudly. The transport is parked now and the block reaches nothing, so the
        value is left as the operator wrote it and only the effect is reported.
        """
        if ssh is not None:
            logger.warning(
                "agents.claude.ssh is not honoured and the run stays local: a run "
                "happens in its thread's workspace, and nothing makes one on %s",
                ssh.host,
            )
        return ssh


class CodexConfig(AgentConfig):
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
    runtime: CodexSdkConfig = Field(
        default_factory=CodexSdkConfig,
        description=(
            "Default SDK runtime config used to launch the local Codex app-server."
        ),
    )
    models: set[CodexModelName] = Field(
        min_length=1,
        description="Codex model route labels this agent exposes to channels.",
    )
    claims: dict[CodexModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned).",
    )
    native_gateway: bool = Field(
        default=True,
        description="Whether anonymous codex-native sessions may cast the served "
        "gateway spells over `/gateway/mcp`. The bearer still authenticates every "
        "call; this is the runtime's own switch, matched against the "
        "X-Octomate-Client header a static install writes.",
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
            "SDK filesystem sandbox preset for a Codex thread: what a command may "
            "touch when nobody is asked. The operator's, and fixed for a run — "
            "deliberately not folded into `permission_mode`, so a conversation's "
            "approval posture never rewrites what the whole thread reaches. A driven "
            "run under `workspace_write` is given the network; `read_only` has no "
            "config key to open it with, so choosing it closes the network too."
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
        default=3600.0,
        description=(
            "Seconds to wait for a human Codex approval/answer before the card "
            "expires and the SDK request is denied. An hour by default, because not "
            "answering is the ordinary case rather than the exotic one, and an "
            "unbounded wait leaves the thread unusable for good. None waits "
            "indefinitely."
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


class DeepseekConfig(AgentConfig):
    """DeepSeek Harness runner, registered as the `deepseek` agent tentacle.

    Opt-in: `agents.deepseek` is null by default, so the agent is absent unless a
    block is supplied. The tentacle attaches to a dsh already serving
    `host:port` — one the operator runs — and starts its own `dsh web` child
    only when nothing answers there. Either way it drives the harness over the
    `/api` gateway — HTTP for unary calls, the mux WebSocket for events — the
    same integration surface dsh's own web client uses. Sessions are
    per-conversation: the dsh session id is stored as the conversation
    `external_id` and prompted again for later turns.
    """

    host: Literal["127.0.0.1", "localhost"] = Field(
        default="127.0.0.1",
        description=(
            "Where a dsh serves `/api` — loopback only, enforced here: the "
            "gateway has no TLS and no auth, and a started child binds loopback, "
            "so a remote host could neither be trusted nor answered. A dsh "
            "already answering here is attached to as it stands."
        ),
    )
    port: int = Field(
        default=3080,
        ge=1,
        le=65535,
        description=(
            "The `/api` port — dsh's own default bind. A started `dsh web` binds "
            "this same port, fixed rather than ephemeral, so the next probe "
            "attaches to it instead of starting a second writer of one DSH_HOME."
        ),
    )
    executable: str = Field(
        default="dsh",
        description=(
            "The dsh command to spawn `dsh web` with when nothing serves "
            "`host:port` — a name resolved on PATH or an absolute path to a "
            "built dsh."
        ),
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            "Extra arguments appended after "
            "`web --host 127.0.0.1 --port <port> --no-open`, e.g. a `--patch` "
            "overlay. Only applies to a harness octomate starts. A dsh that "
            "refuses one of these exits and fails the start — only octomate's "
            "own `--no-open` is dropped and retried."
        ),
    )
    dsh_home: ConfigPath = Field(
        default=Path("~/.dsh"),
        # The default rides through ConfigPath's expanduser like any set value.
        validate_default=True,
        description=(
            "DSH_HOME for a harness octomate starts — where dsh keeps its "
            "sessions and settings. Defaults to dsh's own ~/.dsh; the child "
            "always receives this value verbatim. An attached harness keeps "
            "whatever home it was started with."
        ),
    )
    provider: str = Field(
        default="deepseek-official",
        description=(
            "The dsh provider route the model labels below belong to; "
            "`session.selectModel` sends (provider, model) pairs."
        ),
    )
    models: set[DeepseekModelName] = Field(
        min_length=1,
        description="dsh model route labels this agent exposes to channels.",
    )
    claims: dict[DeepseekModelName, Claim] = Field(
        default_factory=dict,
        description="Per-model claims (ability/efforts). A model with no claim "
        "advertises nothing: it is not offered as a route, so it cannot be "
        "summoned (or commissioned). DeepSeek's efforts collapse to off/high/max, "
        "so claims should offer `[low, medium, high, xhigh]` at most.",
    )
    native_gateway: bool = Field(
        default=True,
        description="Whether anonymous deepseek-native sessions may cast the served "
        "gateway spells over `/gateway/mcp`. The bearer still authenticates every "
        "call; this is the runtime's own switch, matched against the "
        "X-Octomate-Client header a static install writes.",
    )
    efforts: dict[ThinkingEffort, str] = Field(
        default_factory=lambda: {
            "minimal": "off",
            "low": "off",
            "medium": "high",
            "high": "high",
            "xhigh": "max",
        },
        description=(
            "Octomate's one effort vocabulary mapped onto dsh's adapter-owned "
            "reasoning-effort ids. The default fits llm-deepseek (off/high/max); "
            "a deployment routing another adapter overrides it."
        ),
    )
    permission_mode: DeepseekPermissionMode = Field(
        default="workspace-write",
        description=(
            "Permission preset a dsh conversation falls back to when it carries "
            "none of its own. dsh's preset bundles sandbox mode and approval "
            "policy; switched per session via the `/permission` command."
        ),
    )
    agent_preset: str | None = Field(
        default=None,
        description=(
            "Agent preset new sessions are composed from (`session.create`'s "
            "agentPreset). Null takes the deployment's default preset."
        ),
    )
    approval_timeout: float | None = Field(
        default=3600.0,
        description=(
            "Seconds to wait for a human approval/answer before the card expires "
            "and the dsh request is answered `cancelled` (so the turn unblocks). An "
            "hour by default, because not answering is the ordinary case rather than "
            "the exotic one, and an unbounded wait leaves the thread unusable for "
            "good. None waits indefinitely."
        ),
    )
    ready_timeout: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Seconds to wait for the `dsh web:` readiness banner before the "
            "spawn is declared failed."
        ),
    )


class AgentsConfig(BaseModel):
    """Every agent is opt-in and omitting one means it is absent, inkling included.

    Nothing here defaults to a model: which LLM an operator has keys for is not
    something this project can guess, and a defaulted one would be a route that
    boots fine and 401s on first use. Declaring an agent means declaring at least
    one model for it, which each agent's own `models` field enforces.
    """

    inkling: InklingConfig | None = None
    claude: ClaudeCodeConfig | None = None
    codex: CodexConfig | None = None
    deepseek: DeepseekConfig | None = None

    def configured_models(self) -> dict[str, set[str]]:
        """Each connectable agent id and the model names it routes.

        The one place that knows the four slots and their differing `models`
        shapes, so a channel route can be checked — and a tentacle built — without
        anything downstream naming an agent.
        """
        configured: dict[str, set[str]] = {}
        if self.inkling is not None and self.inkling.enabled:
            configured["inkling"] = {model.name for model in self.inkling.models}
        for agent_id, agent in (
            ("claude", self.claude),
            ("codex", self.codex),
            ("deepseek", self.deepseek),
        ):
            if agent is not None and agent.enabled:
                configured[agent_id] = set(agent.models)
        return configured
