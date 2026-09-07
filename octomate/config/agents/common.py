"""Shared agent configuration and routing metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field
from pydantic_ai.settings import ThinkingEffort

type AgentRouteModelName = Annotated[str, Field(min_length=1)]


ThinkingEfforts: tuple[ThinkingEffort, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
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
    efforts: tuple[ThinkingEffort, ...] = ThinkingEfforts

    def __str__(self) -> str:
        return f"[effort {'/'.join(self.efforts) or 'default'}] {self.ability}"


class AgentConfig(BaseModel):
    """What every agent tentacle's config block declares, whichever runtime it
    drives: the agent reads the same way everywhere — declared and enabled, or
    absent — and carries its own half of the gateway switch."""

    id: ClassVar[str]  # Registered tentacle ID, fixed by each agent runtime.

    enabled: bool = Field(
        default=True,
        description="Whether to register the tentacle when its config block exists.",
    )
    gateway: bool = Field(
        default=True,
        description="Whether this agent's driven turns offer the gateway spells — "
        "routing, and binding a thread to a project. Off, no channel connection can "
        "switch them on for it.",
    )
