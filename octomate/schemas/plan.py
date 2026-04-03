"""Lightweight internal plan schema for multi-step task decomposition.

These models are used exclusively inside the tentacle brain — no plan details,
step reasoning, or intermediate artifacts are ever surfaced to the user.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PlanStep(BaseModel):
    """A single step in a lightweight internal plan."""

    index: int
    instruction: str
    output: str = ""
    status: StepStatus = StepStatus.PENDING


class Plan(BaseModel):
    """An ordered sequence of steps that decompose a medium-complexity request.

    The plan is purely internal — the user only sees the final synthesis result.
    """

    goal: str
    steps: list[PlanStep] = Field(default_factory=list)

    @property
    def current_step(self) -> PlanStep | None:
        return next((s for s in self.steps if s.status == StepStatus.PENDING), None)

    @property
    def is_complete(self) -> bool:
        return all(s.status in (StepStatus.DONE, StepStatus.FAILED) for s in self.steps)

    @property
    def failed(self) -> bool:
        return any(s.status == StepStatus.FAILED for s in self.steps)
