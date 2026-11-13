"""Capabilities mounted by the Inkling agent."""

from typing import Protocol, TypeVar, runtime_checkable

from pydantic_ai import AgentCapability

from octomate.schemas.user import UserProfile

# Contravariant, mirroring `AgentCapability`'s own deps parameter.
CapabilityDepsT = TypeVar("CapabilityDepsT", contravariant=True)


@runtime_checkable
class UserScopedCapability(Protocol[CapabilityDepsT]):
    """A capability whose credentials belong to the user a run is answering.

    Mounted on a tentacle like any other capability, but never served to a run as-is:
    the tentacle asks it for the copy bound to that run's user, and mounts that instead.
    `None` means this user gets nothing from it — an unregistered visitor, or a channel
    it does not cover.
    """

    async def for_profile(
        self,
        profile: UserProfile,
    ) -> AgentCapability[CapabilityDepsT] | None: ...


__all__ = ["UserScopedCapability"]
