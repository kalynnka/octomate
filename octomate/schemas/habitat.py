"""Habitat ownership, as core establishes it."""

from __future__ import annotations

from typing import Self

from pydantic import UUID7, BaseModel, ConfigDict

from octomate.schemas.user import UserProfile


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
