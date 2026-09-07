"""Inkling configuration and tool output policies."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, Field
from pydantic_ai.models import KnownModelName
from pydantic_ai_harness.tool_output_limits import TruncationStrategy

from octomate.config.agents.common import AgentConfig, Claim
from octomate.config.models import ModelConfig
from octomate.types.permissions import InklingPermissionMode


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
    TruncateAction | SpillAction | SummarizeAction,
    Field(discriminator="kind"),
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


class InklingConfig(AgentConfig):
    id: ClassVar[str] = "inkling"

    models: list[ModelConfig] = Field(min_length=1)

    request_limit: int = Field(
        default=256,
        gt=0,
        description="Maximum model requests in one Inkling run.",
    )

    claims: dict[KnownModelName, Claim] = Field(
        default_factory=dict,
        description="Optional per-model descriptions and supported thinking efforts. "
        "Every configured model is available to bound channels.",
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
