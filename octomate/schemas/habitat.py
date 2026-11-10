"""Habitat ownership and lifecycle observations published by the core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, Field

from octomate.schemas.user import UserProfile


class HabitatObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID7
    habitat: str
    sequence: int = Field(ge=0, description="Monotonic within this manager lifetime.")
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    invocation_id: UUID7 | None = None
    thread_id: UUID7 | None = None


class HabitatProgress(HabitatObservation):
    event_kind: Literal["habitat_progress"] = "habitat_progress"
    phase: Literal["unknown", "preparing", "starting", "ready", "stopping", "stopped"]


class HabitatFailed(HabitatObservation):
    event_kind: Literal["habitat_failed"] = "habitat_failed"
    phase: Literal["failed"] = "failed"
    message: str = Field(
        description="Operator-actionable text without guest diagnostics."
    )


type HabitatStatus = Annotated[
    HabitatProgress | HabitatFailed, Field(discriminator="event_kind")
]


class InvocationScope(BaseModel):
    """Core-established ownership, never derived from an incoming guest signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID7
    conversation_id: UUID7
    thread_id: UUID7 | None = None

    @classmethod
    def from_profile(
        cls,
        profile: UserProfile,
        *,
        conversation_id: UUID7,
        thread_id: UUID7 | None = None,
    ) -> Self:
        """Accept only a profile already resolved by the core user registry."""
        if profile.user_id is None:
            raise ValueError("Habitat access requires a registered user")
        return cls(
            user_id=profile.user_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )

    def require_binding(self, bound: InvocationScope) -> None:
        """Refuse another owner's private conversation state."""
        if self != bound:
            raise ValueError("Invocation owner or conversation does not match")
