"""Gateway policy: where a conversation can go, decided the same way for every agent.

`GatewaySession` is one turn's gateway — the route and destination resolution, the
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
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    COMMISSION_TOOL_NAME,
    DIRECT_TARGET,
    HERE_TARGET,
    SCHEME_TOOL_NAME,
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    THREAD_TARGET,
    AgentRoute,
    ChannelTarget,
    CrossingLanding,
    Destination,
    GatewayDecision,
    HereLanding,
    SchemeDecision,
    SchemeTarget,
    Scrying,
    SendTarget,
    SummonDecision,
    SummonLanding,
    SummonTarget,
    TeleportDecision,
    TeleportTarget,
    ThreadLanding,
    ThreadTarget,
)
from octomate.types.threads import NATIVE_CHANNEL_USER_ID

if TYPE_CHECKING:
    from pydantic_ai.settings import ThinkingEffort

    from octomate.managers.user import UserManager
    from octomate.schemas.user import UserProfile
    from octomate.tentacles.agents.base import AgentTentacle
    from octomate.tentacles.channels.base import ChannelTentacle

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
class GatewaySession:
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
    # Which agents are live, narrowing `linked_destinations` to channels somebody
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
    # Whether the mounted gateway offers the accomplice spells; `no_landing` names
    # `commission` as the fallback only when it is actually on offer.
    commissioning: bool = field(default=False, init=False)
    decision: GatewayDecision | None = field(default=None, init=False)
    # Every route on this run's own channel but the current agent's own — the info
    # shared with the agent to decide where to go, and what a spell landing here
    # validates a chosen route against. A crossing validates against its own.
    other_routes: list[AgentRoute] = field(init=False, repr=False)
    # `destinations` is computed once per gateway, and a gateway lasts one turn. Held
    # here rather than recomputed because resolving it reaches the identity registry.
    computed_destinations: list[Destination] | None = field(
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
    def allow_sub_thread(self) -> bool:
        """Whether a new sub-thread can be opened from this run's own surface.

        False inside one: every channel that has threads routes them `flat_thread`,
        so a thread is the last one there is. False too where the platform opens
        none at all. Both spells that target a sub-thread ask this, and both refuse
        rather than landing somewhere they did not name.

        True for a gateway with no surface to judge by, as `allow_here` is: refusing
        what it cannot see would block a spell the graph resolves correctly anyway.
        False for a native one, whose terminal is not a surface Octomate can open
        anything from.
        """
        if self.native:
            return False
        address = self.conversation_address
        if address is None:
            return True
        if address.channel_thread_id:
            return False
        channel = self.channels.get(address.channel_tentacle_id)
        return channel is None or channel.surfaces.sub_thread

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

    @property
    def built_in_destinations(self) -> list[Destination]:
        """The places every run has: this chat, and its direct messages. Each is
        offered only where it can actually be reached.

        A sub-thread is not among them. `summon` names one through its own
        `destination` literal, and the spells that resolve a handle — `scheme` and
        `send` — deliver to a person, so every place they can name is somewhere
        private."""
        address = self.conversation_address
        if address is None:
            return []
        built_in: list[Destination] = []
        if self.allow_here:
            built_in.append(
                Destination(
                    handle=HERE_TARGET.handle,
                    label="this conversation",
                    address=address,
                )
            )
        if self.private_blocked_by is None:
            built_in.append(
                Destination(
                    handle=DIRECT_TARGET.handle,
                    label="their direct messages here",
                    address=replace(
                        address,
                        chat_type="dm",
                        chat_id="",
                        channel_thread_id=None,
                        shared=False,
                    ),
                )
            )
        return built_in

    async def destinations(self) -> list[Destination]:
        """Every place this run can name, the built-in ones first.

        One list, so a spell never has its own idea of what a place is — and `scry`
        shows it whole. Computed on first use and kept: most turns never route, so
        the registry is not touched at all unless a spell is actually cast.
        """
        if self.computed_destinations is None:
            self.computed_destinations = (
                self.built_in_destinations + await self.linked_destinations()
            )
        return self.computed_destinations

    async def linked_destinations(self) -> list[Destination]:
        """Their direct messages on other channels they are registered on.

        Only channels that are connected, have direct messages, and serve an agent —
        a place nobody could answer from is not somewhere this can go. Each carries
        the routes *it* runs, because a handoff sent there is resolved against that
        channel's config, not against the one the request came from.
        """
        if self.users is None or self.user_profile is None:
            return []
        linked: list[Destination] = []
        for other in await self.users.linked_profiles(self.user_profile):
            channel = self.channels.get(other.channel_tentacle_id)
            if channel is None or not channel.surfaces.direct_message:
                continue
            if not [
                served
                for served in channel.config.agents
                if self.agents is None or served.agent in self.agents
            ]:
                continue
            linked.append(
                Destination(
                    handle=other.channel_tentacle_id,
                    label=f"their direct messages on {channel.name}",
                    address=ChannelAddress(
                        channel_tentacle_id=other.channel_tentacle_id,
                        chat_type="dm",
                        chat_id="",
                        user_id=other.channel_user_id,
                    ),
                    routes=tuple(
                        self.channel_routes.get(other.channel_tentacle_id, [])
                    ),
                )
            )
        return linked

    async def crossing_destinations(self) -> list[Destination]:
        """The other channels this person is on that a turn can be *moved* to.

        `summon` and `teleport` land in a sub-thread wherever they go, so a channel
        that opens none is not somewhere they can be sent — while `scheme`, which
        lands in the direct messages themselves, still reaches it. A channel running
        nothing this run could name is out for the same reason: the turn would arrive
        with nobody to take it. Both crossing spells ask this; neither may cross to
        the channel it is already on. A native session has no address and every
        place it can reach is a crossing, so it alone crosses from nowhere.
        """
        address = self.conversation_address
        if address is None and not self.native:
            return []
        crossing: list[Destination] = []
        for one in await self.destinations():
            if (
                address is not None
                and one.address.channel_tentacle_id == address.channel_tentacle_id
            ):
                continue
            channel = self.channels.get(one.address.channel_tentacle_id)
            if channel is not None and channel.surfaces.sub_thread and one.routes:
                crossing.append(one)
        return crossing

    async def summon_handles(self) -> list[str]:
        """Every handle `summon` can actually land on from here, in the order the
        model should prefer them: this surface, a sub-thread of it, then anywhere
        else the asker is. Empty means the spell has nowhere to go at all, which is
        what each refusal below says when it has nothing to offer instead."""
        handles = [HERE_TARGET.handle] if self.allow_here else []
        if self.allow_sub_thread:
            handles.append(THREAD_TARGET.handle)
        return handles + [one.handle for one in await self.crossing_destinations()]

    async def teleport_handles(self) -> list[str]:
        """Every handle `teleport` can land on. `here` is not among them at any
        surface — a teleport that stayed put would be the agent simply carrying on.

        A shared surface can only reach its own sub-thread. Everything said here
        comes with a teleport, and on a crossing that would republish what other
        people said into somewhere private on another platform, under this person's
        name alone. A private conversation is already all theirs to move.
        """
        handles = [THREAD_TARGET.handle] if self.allow_sub_thread else []
        address = self.conversation_address
        if address is not None and address.shared:
            return handles
        return handles + [one.handle for one in await self.crossing_destinations()]

    def no_landing(self, handle: str, handles: list[str], *, spell: str) -> str:
        """Why `handle` is nowhere `spell` can land, and what is instead.

        A refused reserved word is told which wall it hit, because the wall is what
        stops the model trying the same door again; an unrecognised one just gets
        the list. An empty list is the dead end — there is no "instead" to offer,
        so the sentence says to answer it in place rather than name a way out.
        """
        if handle == HERE_TARGET.handle:
            why = "Cannot take over a group's main channel in place. "
        elif handle == THREAD_TARGET.handle:
            why = (
                "No sub-thread to open here: this conversation is already a thread, "
                "or the channel opens none. "
            )
        else:
            why = (
                f"No destination {handle!r}: not a channel this person is on that "
                "opens sub-threads. "
            )
        if not handles:
            fallback = (
                f", or `{COMMISSION_TOOL_NAME}` an agent to work it in the background."
                if self.commissioning
                else "."
            )
            return f"{why}`{spell}` has nowhere left to land, so answer it{fallback}"
        return f"{why}Use one of these instead, copied exactly: {', '.join(handles)}."

    async def destination(self, handle: str, *, spell: str) -> Destination:
        """The place `handle` names, or a `GatewayRefusal` listing what it could
        have named. The model never names an address — this is where one comes from.
        """
        places = await self.destinations()
        found = next((one for one in places if one.handle == handle), None)
        if found is not None:
            return found
        available = "\n".join(str(one) for one in places) or "- (none)"
        # A built-in that is missing was withheld for a reason, and the reason is
        # what teaches the model something: say which wall it hit rather than
        # implying the place does not exist.
        why = ""
        if (
            handle == DIRECT_TARGET.handle
            and (blocker := self.private_blocked_by) is not None
        ):
            why = f"{PRIVATE_REFUSALS[blocker]} "
        elif handle == HERE_TARGET.handle and not self.allow_here:
            why = "Cannot take over a group's main channel in place. "
        raise GatewayRefusal(
            f"{why}No such destination {handle!r} for {spell}. Copy one of these "
            f"exactly:\n{available}"
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

    async def scry(self) -> Scrying:
        """What this conversation can reach: the routes here, and everywhere else."""
        return Scrying(routes=self.other_routes, destinations=await self.destinations())

    async def summon(
        self,
        *,
        agent_id: str,
        model: str,
        destination: SummonTarget,
        hint: str,
        reason: str,
        summon: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Validate and record a handoff decision, returning the sentence the
        summoning agent is told. The move itself is the graph's, after the turn."""
        handles = await self.summon_handles()
        if destination.handle not in handles:
            raise GatewayRefusal(
                self.no_landing(destination.handle, handles, spell="summon")
            )
        if agent_id == self.current_agent_id:
            raise GatewayRefusal(
                f"Cannot summon yourself {self.current_agent_id!r}. "
                f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
            )
        landing: SummonLanding = HereLanding()
        # Against the routes of the channel it lands on: an agent is summonable
        # where it is configured, so crossing to another one both widens what can be
        # named and narrows it to what runs there.
        offered: list[AgentRoute] | None = None
        if isinstance(destination, ThreadTarget):
            landing = ThreadLanding()
        elif isinstance(destination, ChannelTarget):
            where = await self.destination(destination.handle, spell="summon")
            landing = CrossingLanding(address=where.address)
            offered = [
                route
                for route in where.routes
                if route.agent_id != self.current_agent_id
            ]
        route = self.claimed_route(
            agent_id, model, effort, spell="summon", offered=offered
        )
        self.decision = SummonDecision(
            action="summon",
            agent_id=route.agent_id,
            model=route.model,
            destination=landing,
            effort=effort,
            hint=hint,
            reason=reason,
            summon=summon,
        )
        return f"Summoning {route.agent_id} ({route.model}) → {destination.handle}."

    async def teleport(
        self,
        *,
        hint: str,
        destination: TeleportTarget = THREAD_TARGET,
    ) -> TeleportDecision:
        """Validate and record a teleport decision — the same agent continuing in a
        new sub-thread. How the move happens is the caller's: Inkling's capability
        turns it into a deferral the graph forks mid-run; a runtime that cannot be
        suspended reports it and the graph forks after its turn.

        A native session is refused before any handle is read: its turn lives in a
        terminal Octomate does not drive, so there is nothing to relocate — only
        work to hand off."""
        if self.native:
            raise GatewayRefusal(
                "This session lives in your terminal — Octomate cannot relocate "
                f"it. `{SUMMON_TOOL_NAME}` an agent to take the work up on a real "
                f"channel, or `{SCHEME_TOOL_NAME}` it into someone's direct "
                "messages."
            )
        handles = await self.teleport_handles()
        if destination.handle not in handles:
            raise GatewayRefusal(
                self.no_landing(destination.handle, handles, spell="teleport")
            )
        crossing = (
            await self.destination(destination.handle, spell="teleport")
            if isinstance(destination, ChannelTarget)
            else None
        )
        if crossing is not None and not any(
            route.agent_id == self.current_agent_id for route in crossing.routes
        ):
            channel = self.channels[crossing.address.channel_tentacle_id]
            raise GatewayRefusal(
                f"{channel.name} does not run you ({self.current_agent_id}), and a "
                f"teleport takes you with it. Carry on here, or `{SUMMON_TOOL_NAME}` "
                "an agent it does run."
            )
        decision = TeleportDecision(
            hint=hint,
            crossing=CrossingLanding(address=crossing.address) if crossing else None,
        )
        self.decision = decision
        return decision

    async def scheme(
        self,
        *,
        hint: str,
        brief: str,
        destination: SchemeTarget = DIRECT_TARGET,
    ) -> str:
        """Validate and record a scheme decision, returning the sentence the
        scheming agent is told. The move itself is the graph's, after the turn."""
        where = await self.destination(destination.handle, spell="scheme")
        self.decision = SchemeDecision(
            hint=hint, brief=brief, destination=where.address
        )
        return f"Taking this to {where.label}."

    async def resolve_send(self, destination: SendTarget) -> ChannelAddress | None:
        """The address a send delivers to, or None for this conversation itself.

        Being in their direct messages already stops a `scheme` — nowhere to move
        the conversation to — but never a send: that *is* where it was asked to go,
        so it lands here rather than being refused."""
        already_there = (
            destination.handle == DIRECT_TARGET.handle
            and self.private_blocked_by == "already_private"
        )
        if destination.handle == HERE_TARGET.handle or already_there:
            return None
        return (await self.destination(destination.handle, spell="send")).address

    def native_handoff(self) -> GatewayHandoffSignal:
        """This native session's recorded decision, packaged to be kicked as its
        own turn. Only a native summon or scheme leaves one, so anything else
        asking is a wiring bug, not a refusal a model could correct from."""
        decision = self.decision
        if not self.native or not isinstance(decision, SummonDecision | SchemeDecision):
            raise RuntimeError("only a native summon or scheme kicks a handoff")
        return GatewayHandoffSignal(
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


class GatewayManager:
    """The live gateway sessions, one per driven turn, keyed by conversation id.

    In-process on purpose: a session is only meaningful while its turn is in
    flight, so a restart rightly forgets them all. An external runtime's tool call
    presents a conversation id, and this is where it finds the session of the turn
    driving it."""

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, GatewaySession] = {}

    def register(self, session: GatewaySession) -> None:
        """Hold the conversation for `session`, first arrival only.

        Nothing else serialises turns of one conversation, so two can overlap; an
        external caller naming the conversation must find exactly one session, and
        a second run racing the first would otherwise have its calls land on the
        first's. So the second is refused outright rather than queued or ignored.
        """
        if session.conversation_id is None:
            raise ValueError("a registered gateway session needs its conversation id")
        holder = self.sessions.get(session.conversation_id)
        if holder is not None and holder is not session:
            raise RuntimeError(
                f"conversation {session.conversation_id} already has a turn at the "
                "gateway; a second run on it is refused until that turn ends"
            )
        self.sessions[session.conversation_id] = session

    def unregister(self, session: GatewaySession) -> None:
        # Only its own entry — a session that was never registered (no conversation
        # id, or refused) removes nothing.
        if (
            session.conversation_id is not None
            and self.sessions.get(session.conversation_id) is session
        ):
            del self.sessions[session.conversation_id]

    def get(self, conversation_id: uuid.UUID) -> GatewaySession | None:
        return self.sessions.get(conversation_id)

    def available_routes(
        self,
        channels: dict[str, ChannelTentacle],
        agents: dict[str, AgentTentacle],
    ) -> dict[str, list[AgentRoute]]:
        """What each channel can route to: every connected agent it exposes, each
        answering with its own routes (from its claims). The channel's (agent,
        model) entries only pick its entry/default models — they do not bound the
        routes.

        The one computation behind both `ReflexDeps.available_routes` and the
        served gateway's ephemeral native session, so a driven turn and a native
        call cannot disagree about what a channel offers."""
        return {
            channel_id: [
                route
                for agent_id in dict.fromkeys(
                    connection.agent
                    for connection in channel.config.agents
                    if connection.agent in agents
                )
                for route in agents[agent_id].routes
            ]
            for channel_id, channel in channels.items()
        }

    @contextmanager
    def driving(self, session: GatewaySession | None) -> Generator[None]:
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
