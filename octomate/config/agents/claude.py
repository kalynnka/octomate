"""Claude Code configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from octomate.config.agents.common import AgentConfig, Claim
from octomate.types.permissions import ClaudePermissionMode


class ClaudeCodeConfig(AgentConfig):
    """Claude Agent SDK runner, selected by `type: claude`.

    Opt-in: the agent is absent unless a block is supplied. The CLI supplies the
    model catalog. `claude` runs as a local subprocess.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["claude"] = "claude"

    enabled: bool = Field(
        default=True,
        description="Whether to register the Claude tentacle when the config block exists.",
    )
    instrument: bool = Field(
        default=False,
        description="Export native Claude OTLP spans to Octomate's Logfire project "
        "under the driving trace.",
    )
    claims: dict[str, Claim] = Field(
        default_factory=dict,
        description="Metadata for models whose harness omits descriptions or effort "
        "capabilities. Keys are provider-qualified model names.",
    )
    permission_mode: ClaudePermissionMode = Field(
        default="default",
        description=(
            "Approval posture for a Claude conversation whose thread is in no "
            "project, or whose project declares none. Handed to the SDK verbatim."
        ),
    )
    max_turns: int | None = None
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
