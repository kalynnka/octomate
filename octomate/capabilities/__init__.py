"""Agent capabilities: a tool surface plus the instructions that explain it.

One module per capability — ask, send, todos, history, gateway, github — each
bundling its toolset, its instructions, and any run events it emits. The machinery
they run inside (the `Agent` subclass, the `StreamEvents` catalog, the react loop,
the deferred-tool protocols, the warm MCP toolset cache) lives in `harness`.
Octomate-specific orchestration (triage) and the concrete agent (inkling) live in
`tentacles.agent`.

Each capability is imported from its own module rather than re-exported here: a run
mounts the few it needs, and reaching one should not pull the whole set — GitHub's
OAuth client included — into the importer's graph. What this package does define is
the one contract a capability can opt into beyond pydantic-ai's own.
"""

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
    the integration does not cover.
    """

    async def for_profile(
        self,
        profile: UserProfile,
    ) -> AgentCapability[CapabilityDepsT] | None: ...


__all__ = ["UserScopedCapability"]
