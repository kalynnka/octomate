"""Codex configuration and SDK option types."""

from __future__ import annotations

from typing import ClassVar, Literal

from openai_codex import CodexConfig as CodexSdkConfig
from pydantic import ConfigDict, Field

from octomate.config.agents.common import AgentConfig, Claim
from octomate.types.permissions import CodexPermissionMode

type CodexPersonality = Literal["none", "friendly", "pragmatic"]


type CodexSandbox = Literal["read_only", "workspace_write", "full_access"]


type CodexReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
]


type CodexReasoningSummary = Literal[
    "auto",
    "concise",
    "detailed",
    "none",
]


class CodexConfig(AgentConfig):
    """OpenAI Codex SDK runner, registered as the `codex` agent tentacle.

    Opt-in: `agents.codex` is null by default, so the agent is absent unless a
    block is supplied. The app-server supplies the model catalog. Most fields below
    are default arguments for Codex SDK calls; the tentacle may compose them with
    per-run overrides before calling the SDK.
    """

    model_config = ConfigDict(extra="ignore")

    id: ClassVar[str] = "codex"

    enabled: bool = Field(
        default=True,
        description="Whether to register the Codex tentacle when the config block exists.",
    )
    instrument: bool = Field(
        default=False,
        description="Export native Codex OTLP spans to Octomate's Logfire project "
        "under the driving trace.",
    )
    runtime: CodexSdkConfig = Field(
        default_factory=CodexSdkConfig,
        description=(
            "Default SDK runtime config used to launch the local Codex app-server."
        ),
    )
    claims: dict[str, Claim] = Field(
        default_factory=dict,
        description="Metadata for models whose harness omits descriptions or effort "
        "capabilities. Keys are provider-qualified model names.",
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
