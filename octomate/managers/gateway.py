"""Gateway policy: where a conversation can go, decided the same way for every agent.

`OctomateSession` is one turn's gateway — the route and destination resolution, the
validation of what a spell names, and the typed decision it records for the reflex
graph to act on after the turn. It speaks no pydantic-ai: the Inkling capability
translates its refusals into `ModelRetry` and its teleport into a deferral, and any
other runtime's adapter translates them into its own tool errors, so every runtime
meets the same policy through its own tool mechanism. `GatewayManager` is the
in-process registry an external runtime's tool call resolves its driving turn's
session from.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, overload

from octomate.managers.base import Manager
from octomate.managers.workspaces.mirrors import run_git
from octomate.schemas.awakes import GatewayNativeSignal, GatewayThreadSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.operations import (
    OperationAvailability,
    ThreadOperations,
)
from octomate.schemas.thread import ATTRIBUTABLE_KINDS
from octomate.schemas.triage import (
    DIRECT_TARGET,
    INSPECT_TOOL_NAME,
    SUMMON_TOOL_NAME,
    AgentRoute,
    DirectTarget,
    GatewayDecision,
    HereTarget,
    InspectFacet,
    ProjectSummary,
    SchemeDecision,
    SchemeTarget,
    SendTarget,
    SummonDecision,
    TeleportDecision,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent import AgentTentacle
from octomate.types.threads import NATIVE_CHANNEL_USER_ID

if TYPE_CHECKING:
    from pydantic_ai.settings import ThinkingEffort

    from octomate.managers.thread import ThreadManager
    from octomate.managers.user import UserManager
    from octomate.managers.workspaces import WorkspaceManager
    from octomate.tentacles.channel import ChannelTentacle

# Why `scheme` has nowhere to land, and the sentence each reason refuses with.
PrivateBlocker = Literal["no_surface", "already_private", "no_user"]
PRIVATE_REFUSALS: dict[PrivateBlocker, str] = {
    "no_surface": "This channel has no direct messages.",
    "already_private": (
        "This conversation is already that user's direct messages, so there is "
        "nowhere to move it to."
    ),
    "no_user": "This run has no single user whose direct messages could be opened.",
}


class GatewayRefusal(Exception):
    """A spell that cannot proceed as named, with the sentence that teaches the
    caller what to name instead. Neutral on purpose: the Inkling capability
    re-raises it as `ModelRetry`, and an MCP surface returns it as a tool error
    the model corrects from — one refusal, worded once, for every runtime."""


@dataclass
class OctomateSession:
    """One turn's gateway: the policy the spells share, and the decision they record.

    Built per turn by whoever drives one — the react node for a driven agent — and
    read again when the turn ends: a deciding spell stores its typed decision here,
    and the reflex graph performs the move. The spells themselves are projected onto
    each runtime's own tool mechanism by a thin adapter that owns nothing but the
    translation.
    """

    # What each channel can route to, keyed by channel id — not one list, because a
    # spell that crosses lands on a channel with its own idea of who runs there.
    # This run's own channel answers `routes`; the rest answer a crossing.
    channel_routes: dict[str, list[AgentRoute]]
    current_agent_id: str
    # Every connected channel, so `surfaces` can be read for any of them and not
    # just this run's own — what a cross-channel move needs.
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    # The identity registry and the run's own profile: together they say where else
    # this person is reachable. Both None on a gateway built only to route locally.
    users: UserManager | None = None
    user_profile: UserProfile | None = None
    # Which agents are live, narrowing destinations to channels somebody
    # actually serves; also what the accomplice spells run with.
    agents: dict[str, AgentTentacle] | None = None
    thread_id: uuid.UUID | None = None
    # Where this run lives; also what `allow_here` and `private_blocked_by` read.
    conversation_address: ChannelAddress | None = None
    # The conversation whose turn this session belongs to — the key an external
    # runtime's tool call presents to `GatewayManager`. None on a gateway built
    # outside a driven turn, which is then never registered.
    conversation_id: uuid.UUID | None = None
    # An anonymous native session — a terminal run reaching the served gateway with
    # a runtime attribution and nothing else. Policy, never schema: there is no
    # here or sub-thread to land on, every destination is a crossing, and a summon
    # or scheme is kicked as its own turn instead of being read after this one.
    native: bool = False
    # What the project spells work with — a `teleport` that binds, `dismiss`, the
    # `projects` facet: the ledger a binding is written to and read from, and the
    # registry and forks. Both None on a gateway built only to route.
    threads: ThreadManager | None = None
    workspaces: WorkspaceManager | None = None
    decision: GatewayDecision | None = field(default=None, init=False)
    # Whether `dismiss` was cast: the thread's workspace goes when the turn ends,
    # the graph's doing once the run is out of it.
    dismissing: bool = field(default=False, init=False)
    # Every route on this run's own channel but the current agent's own — the info
    # shared with the agent to decide where to go, and what a spell landing here
    # validates a chosen route against. A crossing validates against its own.
    other_routes: list[AgentRoute] = field(init=False, repr=False)

    # Discovery is lazy and reused for this gateway session.
    destination_cache: list[ChannelAddress] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        address = self.conversation_address
        here = (
            self.channel_routes.get(address.channel_tentacle_id, [])
            if address is not None
            else []
        )
        self.other_routes = [
            route for route in here if route.agent_id != self.current_agent_id
        ]

    @property
    def allow_here(self) -> bool:
        """Whether `summon here` may hand this conversation over in place.

        False on a group's main channel, where pinning an owner would route every
        gated-in message, from any user, to one agent — and false for a native
        session, which has no conversation Octomate could hand over."""
        if self.native:
            return False
        address = self.conversation_address
        if address is None:
            return True
        return address.chat_type != "group"

    @property
    def private_blocked_by(self) -> PrivateBlocker | None:
        """Why `scheme` has nowhere to land from this run, or None when it does.

        A gateway that knows no channels can reach no direct messages."""
        address = self.conversation_address
        if address is None:
            return "no_user"
        channel = self.channels.get(address.channel_tentacle_id)
        if channel is None or not channel.surfaces.direct_message:
            return "no_surface"
        # Read the surface, not the type: a Slack assistant pane and a Lark p2p topic
        # are threads that only one person can read, and moving them to "their direct
        # messages" would land beside where they already are, under another owner.
        if not address.shared:
            return "already_private"
        if not address.user_id:
            return "no_user"
        return None

    async def destinations(self) -> list[ChannelAddress]:
        """Cache optional address suggestions; execution validates its address independently."""
        if self.destination_cache is not None:
            return self.destination_cache
        linked = (
            await self.users.linked_profiles(self.user_profile)
            if self.users is not None and self.user_profile is not None
            else []
        )
        profiles = {profile.channel_tentacle_id: profile for profile in linked}
        if self.user_profile is not None:
            profiles[self.user_profile.channel_tentacle_id] = self.user_profile
        source = self.conversation_address
        if source is not None and source.channel_tentacle_id not in profiles:
            profiles[source.channel_tentacle_id] = UserProfile(
                channel_tentacle_id=source.channel_tentacle_id,
                channel_user_id=source.user_id,
            )
        addresses: list[ChannelAddress] = []
        if source is not None and self.private_blocked_by is None:
            addresses.append(
                ChannelAddress(source.channel_tentacle_id, "dm", "", source.user_id)
            )
        for channel in self.channels.values():
            profile = profiles.get(channel.id, self.user_profile)
            if profile is None:
                continue
            user_id = channel.thread_user_id(profile)
            if (
                user_id is not None
                and channel.surfaces.direct_message
                and any(
                    self.agents is None or agent_id in self.agents
                    for agent_id in channel.agent_ids
                )
                and (
                    source is None
                    or source.channel_tentacle_id != channel.id
                    or self.private_blocked_by is None
                )
            ):
                addresses.append(ChannelAddress(channel.id, "dm", "", user_id))
            if self.channel_routes.get(channel.id):
                addresses.extend(await channel.suggest_addresses(profile, source))
        self.destination_cache = list(dict.fromkeys(addresses))
        return self.destination_cache

    @property
    async def operations(self) -> ThreadOperations:
        """Offer addresses and channel routes without introducing another target type."""
        suggestions = [
            address
            for address in await self.destinations()
            if address.chat_type == "thread"
            or self.channels[address.channel_tentacle_id].accepts_sub_thread(address)
        ]
        source = self.conversation_address
        routes = {
            channel: [
                route for route in routes if route.agent_id != self.current_agent_id
            ]
            for channel, routes in self.channel_routes.items()
        }
        here = (
            source
            if self.allow_here
            and source is not None
            and routes.get(source.channel_tentacle_id)
            else None
        )
        summon = [one for one in suggestions if routes.get(one.channel_tentacle_id)]
        reason = self.teleport_unavailable
        teleport = [
            one
            for one in suggestions
            if not reason
            and (source is None or not source.shared or one == source)
            and (
                one == source
                or any(
                    self.is_compatible_agent(route.agent_id)
                    for route in self.channel_routes.get(one.channel_tentacle_id, [])
                )
            )
        ]
        return ThreadOperations(
            teleport=OperationAvailability(
                destinations=teleport,
                reason=reason
                or (
                    None
                    if teleport
                    else "No eligible destinations for this agent and history."
                ),
            ),
            summon=OperationAvailability(
                destinations=summon,
                here=here,
                routes=routes,
                reason=None
                if summon or here
                else "No other agents are available at an eligible destination.",
            ),
        )

    def is_compatible_agent(self, agent_id: str) -> bool:
        """A driven source keeps its agent; native history needs a matching importer."""
        if not self.native:
            return agent_id == self.current_agent_id
        agent = self.agents.get(agent_id) if self.agents is not None else None
        return (
            agent is not None
            and agent.native_id == self.current_agent_id
            and type(agent).fork is not AgentTentacle.fork
        )

    @property
    def teleport_unavailable(self) -> str | None:
        """Only a stored, owned history can be imported from a native runtime."""
        if self.native and self.thread_id is None:
            return "Native teleport requires an uploaded thread; use Trunkline to select it."
        if not self.native and self.agents is not None:
            agent = self.agents.get(self.current_agent_id)
            if agent is None:
                return "The source agent is not connected."
            if not agent.supports_session_fork:
                return "The source agent does not support independent session forking."
        return None

    async def channel_profile(self, channel: ChannelTentacle) -> UserProfile:
        """The requester's identity usable by this channel, never supplied by a tool."""
        profile = self.user_profile
        source = self.conversation_address
        if (
            profile is None
            and source is not None
            and source.channel_tentacle_id == channel.id
        ):
            profile = UserProfile(
                channel_tentacle_id=channel.id, channel_user_id=source.user_id
            )
        if profile is not None and channel.thread_user_id(profile) is not None:
            return profile
        if profile is not None and self.users is not None:
            linked = await self.users.linked_profiles(profile)
            found = next(
                (one for one in linked if one.channel_tentacle_id == channel.id), None
            )
            if found is not None:
                return found
        raise GatewayRefusal("Link your profile on the destination channel first.")

    async def prepare_address(self, destination: ChannelAddress) -> ChannelAddress:
        """Verify identity, then let the owning channel validate its address."""
        channel = self.channels.get(destination.channel_tentacle_id)
        if channel is None:
            raise GatewayRefusal(
                f"No connected channel {destination.channel_tentacle_id!r}."
            )
        profile = await self.channel_profile(channel)
        if destination.user_id != channel.thread_user_id(profile):
            raise GatewayRefusal("The address does not belong to the requesting user.")
        try:
            return await channel.prepare_address(destination, self.conversation_address)
        except ValueError as error:
            raise GatewayRefusal(str(error)) from error

    async def direct_destination(self, target: DirectTarget) -> ChannelAddress:
        """Resolve the requesting user's DM on the selected channel."""
        source = self.conversation_address
        local = target.channel is None or (
            source is not None and target.channel == source.channel_tentacle_id
        )
        if local and (blocker := self.private_blocked_by) is not None:
            raise GatewayRefusal(PRIVATE_REFUSALS[blocker])
        channel_id = target.channel or (source.channel_tentacle_id if source else "")
        channel = self.channels.get(channel_id)
        if channel is None:
            raise GatewayRefusal(f"No connected channel {channel_id!r}.")
        if not channel.surfaces.direct_message:
            raise GatewayRefusal("This channel has no direct messages.")
        if not local and not any(
            self.agents is None or agent_id in self.agents
            for agent_id in channel.agent_ids
        ):
            raise GatewayRefusal("No connected agent serves the destination channel.")
        profile = await self.channel_profile(channel)
        return ChannelAddress(
            channel_tentacle_id=channel.id,
            chat_type="dm",
            chat_id="",
            user_id=profile.channel_user_id,
        )

    def claimed_route(
        self,
        agent_id: str,
        model: str,
        effort: ThinkingEffort | None,
        *,
        spell: str,
        offered: list[AgentRoute] | None = None,
    ) -> AgentRoute:
        """The offered route for (agent_id, model), with the requested effort
        validated against its claim — the shared gatekeeping of `summon` and
        `commission`. Both arrive as free strings, so this is where an unrouteable
        pair is caught; callers build from the returned route, never from the args.

        `offered` is the list to check against, defaulting to this channel's. A
        summon that crosses passes the far channel's, since that is who will be
        asked to run it."""
        offered = self.other_routes if offered is None else offered
        route = next(
            (
                route
                for route in offered
                if route.agent_id == agent_id and str(route.model) == model
            ),
            None,
        )
        if route is None:
            available = "\n".join(str(route) for route in offered) or "- (none)"
            raise GatewayRefusal(
                f"Invalid {spell} route (agent_id={agent_id!r}, "
                f"model={model!r}). Copy an agent_id and model exactly "
                f"from one of these routes:\n{available}"
            )
        if effort is not None and effort not in route.claim.efforts:
            raise GatewayRefusal(
                f"Route (agent_id={agent_id!r}, model={model!r}) does "
                f"not accept effort {effort!r}; it claims "
                f"{'/'.join(route.claim.efforts)}. Pick one of those, or omit "
                f"effort."
            )
        return route

    @overload
    async def inspect(
        self, reveal: Literal["routes"], channel: str | None = None
    ) -> list[AgentRoute]: ...

    @overload
    async def inspect(
        self, reveal: Literal["destinations"], channel: str | None = None
    ) -> list[ChannelAddress]: ...

    @overload
    async def inspect(
        self, reveal: Literal["projects"], channel: str | None = None
    ) -> list[ProjectSummary]: ...

    async def inspect(
        self, reveal: InspectFacet, channel: str | None = None
    ) -> list[AgentRoute] | list[ChannelAddress] | list[ProjectSummary]:
        """Reveal current or selected-channel routes, suggested addresses, or projects.

        Only the requested facet is computed; address discovery consults linked
        identities and does not limit which addresses execution can validate.
        """
        if channel is not None:
            if reveal != "routes":
                raise GatewayRefusal(
                    "Only routes can be inspected for a specific channel."
                )
            target = self.channels.get(channel)
            if target is None:
                raise GatewayRefusal(f"No connected channel {channel!r}.")
            await self.channel_profile(target)
            return [
                route
                for route in self.channel_routes.get(channel, [])
                if route.agent_id != self.current_agent_id
            ]
        match reveal:
            case "routes":
                return self.other_routes
            case "destinations":
                return await self.destinations()
            case "projects":
                return await self.projects()

    async def projects(self) -> list[ProjectSummary]:
        """The projects this deployment can work on — a registered user's to see,
        being theirs to bind; a visitor is refused rather than shown nothing."""
        if (
            self.users is None
            or self.user_profile is None
            or await self.users.owner(self.user_profile) is None
        ):
            raise GatewayRefusal(
                "This session speaks for no registered user, and the projects are a "
                "registered user's to see."
            )
        if self.workspaces is None:
            raise RuntimeError("the project facet needs the workspace manager")
        return [
            ProjectSummary(name=project.name, description=project.description)
            for project in self.workspaces.projects.list()
            if project.enabled
        ]

    async def summon(
        self,
        *,
        agent_id: str,
        model: str,
        destination: ChannelAddress | None = None,
        new_thread: bool = True,
        hint: str,
        reason: str,
        summon: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Validate an address and record a handoff for the graph to perform."""
        if agent_id == self.current_agent_id:
            raise GatewayRefusal(
                f"Cannot summon yourself {self.current_agent_id!r}. "
                f'Call `{INSPECT_TOOL_NAME}` with `reveal="routes"` to choose a valid route.'
            )
        destination = destination or self.conversation_address
        if destination is None:
            raise GatewayRefusal("Summon requires a destination address.")
        if new_thread:
            destination = await self.prepare_address(destination)
        elif (
            self.native
            or not self.allow_here
            or destination != self.conversation_address
        ):
            raise GatewayRefusal(
                "Only the current conversation can be taken over in place."
            )
        route = self.claimed_route(
            agent_id,
            model,
            effort,
            spell="summon",
            offered=[
                route
                for route in self.channel_routes.get(
                    destination.channel_tentacle_id, []
                )
                if route.agent_id != self.current_agent_id
            ],
        )
        self.decision = SummonDecision(
            agent_id=route.agent_id,
            model=route.model,
            destination=destination,
            new_thread=new_thread,
            effort=effort,
            hint=hint,
            reason=reason,
            summon=summon,
        )
        return f"Summoning {route.agent_id} ({route.model}) → {destination}."

    async def teleport(
        self,
        *,
        hint: str,
        destination: ChannelAddress | None = None,
        new_thread: bool = True,
        project: str | None = None,
        ref: str | None = None,
    ) -> TeleportDecision:
        """Validate and record a teleport decision — the same agent continuing
        somewhere else, its history with it. How the move happens is the graph's:
        the run ends on the decision and the Teleport node performs it.

        A `project` makes the move one into that project's workspace: the thread
        landed in is bound to it — a new sub-thread or crossing, or this thread
        when `new_thread` is false, the one case a teleport may stay put. The
        project and the ref are validated here, where a refusal reaches the model;
        the binding itself is the graph's, on the thread that turns out to be
        landed in. Binding is a trust act (a project's own `AGENTS.md` reaches the
        agent as instructions), so it takes a registered user; only a thread binds,
        and once — so staying put is refused for a DM or a group and for a thread
        already about a project, before a mirror is synced for nothing.

        Native history requires a stored thread and a runtime that can import its
        uploaded transcript. Anonymous native tool sessions still cannot teleport."""
        agent_id = self.current_agent_id
        if self.native and (reason := self.teleport_unavailable):
            raise GatewayRefusal(reason)
        destination = destination or self.conversation_address
        if destination is None:
            raise GatewayRefusal("Teleport requires a destination address.")
        if self.native and not new_thread:
            raise GatewayRefusal("Native teleport requires a new destination.")
        if project is not None and (
            self.users is None
            or self.user_profile is None
            or await self.users.owner(self.user_profile) is None
        ):
            raise GatewayRefusal(
                "This session speaks for no registered user, and binding a thread to "
                "a project is a registered user's act."
            )
        if not new_thread:
            if destination != self.conversation_address:
                raise GatewayRefusal("Only the current conversation can be reused.")
            if project is None:
                raise GatewayRefusal(
                    "A teleport that stays put is you carrying on. Name a project to "
                    "bind this thread to, or somewhere to go."
                )
            if self.thread_id is None or self.threads is None:
                raise RuntimeError(
                    "a teleport that binds this thread needs the thread this turn "
                    "is in, and the ledger it is written to"
                )
            thread = await self.threads.get(self.thread_id)
            if thread is None:
                raise RuntimeError(f"thread {self.thread_id} vanished")
            if thread.kind not in ATTRIBUTABLE_KINDS:
                raise GatewayRefusal(
                    f"This conversation is a {thread.kind}, and a DM or a group chat "
                    "outlives every project in it — only a thread binds. Teleport "
                    f"with `new_thread=true` to open one about {project!r}."
                )
            current = await thread.project
            if current is not None:
                raise GatewayRefusal(
                    f"This thread is already about {current.name!r}, and a thread "
                    "binds once — a different project is a different thread, which "
                    "`new_thread=true` opens."
                )
        else:
            source = self.conversation_address
            if source is not None and source.shared and destination != source:
                raise GatewayRefusal(
                    "Shared history may only move into a sub-thread of the current chat."
                )
            destination = await self.prepare_address(destination)
        if new_thread and (self.native or destination != self.conversation_address):
            route = next(
                (
                    route
                    for route in self.channel_routes.get(
                        destination.channel_tentacle_id, []
                    )
                    if self.is_compatible_agent(route.agent_id)
                ),
                None,
            )
            if route is None:
                channel = self.channels[destination.channel_tentacle_id]
                raise GatewayRefusal(
                    f"{channel.name} has no agent that can import this native history."
                    if self.native
                    else f"{channel.name} does not run you ({self.current_agent_id}), "
                    f"and a teleport takes you with it. Carry on here, or "
                    f"`{SUMMON_TOOL_NAME}` an agent it does run."
                )
            agent_id = route.agent_id
        if reason := self.teleport_unavailable:
            raise GatewayRefusal(reason)
        if project is not None:
            if self.workspaces is None:
                raise RuntimeError("a teleport into a project needs the workspaces")
            registered = self.workspaces.projects.get(project)
            if registered is None or not registered.enabled:
                available = sorted(
                    other.name
                    for other in self.workspaces.projects.list()
                    if other.enabled
                )
                raise GatewayRefusal(
                    f"No project called {project!r} is registered here. "
                    f"Available: {', '.join(available) or 'none'}."
                )
            # Ahead of the move, because a thread binds once: a ref that turns out
            # not to resolve would otherwise leave the thread committed to the
            # project with no way to ask again for the branch that was meant.
            mirror = await self.workspaces.mirrors.sync(registered)
            if ref is not None and not await run_git("ls-remote", str(mirror), ref):
                raise GatewayRefusal(
                    f"{registered.name!r} has no {ref!r} to start from. Name a "
                    "branch, tag or commit its mirror has, or omit it for the "
                    "default branch."
                )
        decision = TeleportDecision(
            agent_id=agent_id,
            hint=hint,
            destination=destination,
            new_thread=new_thread,
            project=project,
            ref=ref,
        )
        self.decision = decision
        return decision

    async def dismiss(self) -> str:
        """Record that this thread's workspace may go: its agent has said the work
        in it is done. Validated here, where a refusal reaches the model; the
        release is the graph's, once the turn ends and its work is saved, so no
        tree is pulled out from under a run still in it.

        No registered user is needed, unlike a binding: what a release costs is a
        fork on the next turn, never work. A thread about no project has nothing
        to release — its tree goes with the turn — and a native session runs in a
        directory of its own that Octomate never forked."""
        if self.native:
            raise GatewayRefusal(
                "This session lives in your terminal, in a directory of its own — "
                "Octomate forked nothing for it, and has nothing to release."
            )
        if self.thread_id is None or self.threads is None:
            raise RuntimeError(
                "a dismiss needs the thread this turn is in, and the ledger it is "
                "read from"
            )
        thread = await self.threads.get(self.thread_id)
        if thread is None:
            raise RuntimeError(f"thread {self.thread_id} vanished")
        if await thread.project is None:
            raise GatewayRefusal(
                "This thread is about no project, and its tree is thrown away when "
                "this turn ends — there is nothing to release."
            )
        self.dismissing = True
        return (
            "Releasing this thread's workspace when this turn ends, once its work is "
            "saved to the project's mirror. Nothing is lost — a later message here "
            "forks it afresh from there — but finish up now: your working directory "
            "goes with it."
        )

    async def scheme(
        self,
        *,
        hint: str,
        brief: str,
        destination: SchemeTarget = DIRECT_TARGET,
    ) -> str:
        """Validate and record a scheme decision, returning the sentence the
        scheming agent is told. The move itself is the graph's, after the turn."""
        address = await self.direct_destination(destination)
        self.decision = SchemeDecision(hint=hint, brief=brief, destination=address)
        return f"Taking this to your direct messages on {address.channel_tentacle_id}."

    async def resolve_send(self, destination: SendTarget) -> ChannelAddress | None:
        """The address a send delivers to, or None for this conversation itself.

        Being in their direct messages already stops a `scheme` — nowhere to move
        the conversation to — but never a send: that *is* where it was asked to go,
        so it lands here rather than being refused."""
        if isinstance(destination, HereTarget):
            return None
        source = self.conversation_address
        local = destination.channel is None or (
            source is not None and destination.channel == source.channel_tentacle_id
        )
        if local and self.private_blocked_by == "already_private":
            return None
        return await self.direct_destination(destination)

    def thread_operation(self) -> GatewayThreadSignal:
        """Package a validated user action for the graph's existing-thread entry."""
        if (
            self.thread_id is None
            or self.conversation_address is None
            or self.user_profile is None
            or not isinstance(self.decision, SummonDecision | TeleportDecision)
        ):
            raise ValueError(
                "A thread operation requires a thread, user and validated decision."
            )
        return GatewayThreadSignal(
            thread_id=self.thread_id,
            source=self.conversation_address,
            agent_id=self.current_agent_id,
            user_profile=self.user_profile,
            decision=self.decision,
        )

    def native_handoff(self) -> GatewayNativeSignal:
        """This native session's recorded decision, packaged to be kicked as its
        own turn. Only a native summon or scheme leaves one, so anything else
        asking is a wiring bug, not a refusal a model could correct from."""
        decision = self.decision
        if not self.native or not isinstance(decision, SummonDecision | SchemeDecision):
            raise RuntimeError("only a native summon or scheme kicks a handoff")
        return GatewayNativeSignal(
            decision=decision,
            agent_id=self.current_agent_id,
            user_profile=self.user_profile,
            source=ChannelAddress(
                channel_tentacle_id=self.current_agent_id,
                chat_type="dm",
                chat_id="",
                user_id=NATIVE_CHANNEL_USER_ID,
            ),
        )


class GatewayManager(Manager):
    """The live Octomate sessions, one per driven turn, keyed by conversation id.

    In-process on purpose: a session is only meaningful while its turn is in
    flight, so a restart rightly forgets them all. An external runtime's tool call
    presents a conversation id, and this is where it finds the session of the turn
    driving it."""

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, OctomateSession] = {}

    def register(self, session: OctomateSession) -> None:
        """Hold the conversation for `session`, first arrival only.

        Nothing else serialises turns of one conversation, so two can overlap; an
        external caller naming the conversation must find exactly one session, and
        a second run racing the first would otherwise have its calls land on the
        first's. So the second is refused outright rather than queued or ignored.
        """
        if session.conversation_id is None:
            raise ValueError("a registered Octomate session needs its conversation id")
        holder = self.sessions.get(session.conversation_id)
        if holder is not None and holder is not session:
            raise RuntimeError(
                f"conversation {session.conversation_id} already has a turn at the "
                "gateway; a second run on it is refused until that turn ends"
            )
        self.sessions[session.conversation_id] = session

    def unregister(self, session: OctomateSession) -> None:
        # Only its own entry — a session that was never registered (no conversation
        # id, or refused) removes nothing.
        if (
            session.conversation_id is not None
            and self.sessions.get(session.conversation_id) is session
        ):
            del self.sessions[session.conversation_id]

    def get(self, conversation_id: uuid.UUID) -> OctomateSession | None:
        return self.sessions.get(conversation_id)

    def available_routes(
        self,
        channels: dict[str, ChannelTentacle],
        agents: dict[str, AgentTentacle],
    ) -> dict[str, list[AgentRoute]]:
        """What each channel can route to: every connected agent it exposes, each
        answering with its own discovered or configured model routes.

        The one computation behind both `ReflexDeps.available_routes` and the
        served gateway's ephemeral native session, so a driven turn and a native
        call cannot disagree about what a channel offers."""
        return {
            channel_id: [
                route
                for agent_id in dict.fromkeys(
                    connection
                    for connection in channel.agent_ids
                    if connection in agents
                )
                for route in agents[agent_id].routes
            ]
            for channel_id, channel in channels.items()
        }

    @asynccontextmanager
    async def driving(self, session: OctomateSession | None) -> AsyncGenerator[None]:
        """The registration span of one driven turn: external tool calls reach
        `session` only while the run that mounted it is in flight, and a second
        turn of the same conversation is refused at the door. Tolerates a gateway
        that was never built (disabled connection) or never got a conversation id
        (no thread), which is simply not registered."""
        if session is not None and session.conversation_id is not None:
            self.register(session)
        try:
            yield
        finally:
            if session is not None:
                self.unregister(session)
