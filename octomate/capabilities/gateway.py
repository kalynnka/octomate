"""Gate capability: an agent's routing spellbook.

Five spells decide where a turn goes and who handles it. Each is opaque on its own,
so the instruction opens with plain words for what they actually do:

- `scry`: reveal the other agents this one can hand off to or put to work.
- `summon`: hand the conversation to another agent (a handoff — they take over from a
  brief). The graph reads the recorded decision after the run.
- `teleport`: continue the same agent in a new place (a sub-thread), carrying the
  history forward. Deferred, so the graph can fork the history and resume.
- `commission`: draw another agent into working a self-contained task in the background
  and return its report — an ordinary awaited tool call, never a deferral; the caller
  keeps the conversation and the user sees none of it.
- `whisper`: a quiet follow-up to an accomplice by name; it keeps its own context.
- `send`: deliver content mid-run without ending the turn, here or in the
  asking user's direct messages. It lives here rather than in its own capability
  because naming somewhere other than "here" is a routing decision, and this is
  where the channel registry and the run's own address already are.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

from pydantic_ai import AgentStreamEvent, CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ToolReturn,
    ToolReturnPart,
)
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.harness.events import MessageSentEvent
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.messages import SEND_TOOL_NAME
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import (
    COMMISSION_TOOL_NAME,
    DIRECT_TARGET,
    GATEWAY_TOOLSET_ID,
    HERE_TARGET,
    SCHEME_TOOL_NAME,
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_DEFER_KIND,
    TELEPORT_TOOL_NAME,
    THREAD_TARGET,
    WHISPER_TOOL_NAME,
    AgentRoute,
    ChannelTarget,
    CrossingLanding,
    Destination,
    HereLanding,
    SchemeDecision,
    SchemeTarget,
    Scrying,
    SendTarget,
    SummonDecision,
    SummonLanding,
    SummonTarget,
    TeleportTarget,
    ThreadLanding,
    ThreadTarget,
)

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from octomate.managers.conversation import ConversationManager
    from octomate.managers.user import UserManager
    from octomate.schemas.user import UserProfile
    from octomate.tentacles.agents.base import AgentTentacle

    # Runtime import would cycle: `channel.base` reaches this module through
    # `feelers.output`, which skips the accomplice spells' timeline rows by name.
    from octomate.tentacles.channels.base import ChannelTentacle

# A commission holds the parent's live tool call open while the accomplice runs, so the
# wait must not be unbounded (`approval_timeout` is the precedent). Seconds.
COMMISSION_TIMEOUT = 900.0
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

GATE_INSTRUCTION = """\
## Gate — decide where this conversation goes and who handles it

These tools route the conversation. Default to handling it yourself: if you can answer
well or do the work, do it and call none of them. Routing is the exception — reach for a
tool only when one of the signals below clearly fires.

### `summon` — hand off to another agent
Summon transfers the conversation to a specialist who takes over this turn *and its
follow-ups*: a real, sticky handoff, so the bar is high. Summon only when:
- The request needs a capability you lack — e.g. running or editing code in a real
  repository or environment, or a domain another agent is described for.
- It is substantial specialist work another agent would do markedly better, not
  something you can handle from what you already know.

Do NOT summon when:
- You can already answer or do it — length or a technical-sounding topic is not a reason.
- You are only mildly unsure — ask the user a clarifying question instead.
- No route clearly fits — handle it yourself or ask; never summon on a guess.

When one fires, call `scry` first to see the agents and what each is for. Every route
carries a claim: its ability (what that agent+model is for) and the effort levels it
accepts — pick the route whose ability covers the work. Set `effort` only when the
user explicitly asked for a level; otherwise leave it unset so the agent's own default
applies. Then `summon` — copying its `agent_id` and `model` exactly from that route,
and writing a self-contained brief since the other agent may not see this chat.
Choose `destination`: `here` hands over this same conversation; `thread` opens a new
sub-thread of the current chat; a channel id from `scry` opens one in that person's
direct messages on that channel, for work that belongs where they actually do it.
You yourself are not a valid summon target.

### `teleport` — relocate yourself
Move this conversation into a new sub-thread that *you* keep handling, carrying
everything said so far. Use it for multi-step or long-running work that deserves its
own thread but that you are the right one to do — no other agent involved.
`destination` is `thread`, a sub-thread of the current chat, unless you name a channel
id from `scry` to carry it into their direct messages there — offered only from a
conversation nobody else can read, since everything said here travels with you.

### `scheme` — take it to the user privately
Continue one-to-one with the person who asked, in their direct messages: for work that
is theirs alone, or that does not belong in front of the group. Whoever already handles
their direct messages picks it up, so write `brief` self-contained — it may not be you,
and they cannot see this chat. `hint` opens the conversation over there and is the
first thing they read; nothing is posted here, so close out your own reply by saying
the work is moving, not what it is. Only from a group, on a platform that has direct
messages; the tool says so when it does not apply.
"""

# The framing every accomplice run carries, passed by the gateway at the
# `subagent_run` call site: no gate, no user, no approvals — the reply is the
# report.
ACCOMPLICE_INSTRUCTION = """\
You are an accomplice: another agent commissioned you, and your reply is
your report back to it. There is no user here to ask, and anything that needs a
human approval is declined immediately — work from the brief you were given within
what you can do unaided; if something is under-specified or unapprovable, state the
assumption or the blocker in your report and proceed.
"""

SEND_INSTRUCTION = """\

### `send` — deliver something now, without ending your turn
For a progress update, an intermediate result, or an image/file you produced along
the way. Anything sent this way is already delivered: your final reply continues from
there — summarize or extend it, never restate it. If everything worth saying went out
already, close with a short wrap-up rather than re-sending it.

`destination` is `here` by default. `dm` delivers to the person who asked, privately —
for something that is *for them*, like a summary sent over; say in your reply that you
sent it. `dm` hands nothing over: you keep this conversation and nobody picks the work
up there, so use `scheme` when the work itself should continue privately. Asking for
`dm` while already in that person's direct messages is fine — it lands here.

To thread onto a specific message in a busy chat, lead with a reply segment whose id
is that message's `#msg:<id>` handle — it must be the first segment. To ping someone,
use an `at` segment with their user id.
"""

COMMISSION_INSTRUCTION = """\

### `commission` — put another agent to work in the background (you keep the conversation)
Where `summon` hands the conversation away, `commission` does not: another agent works a
self-contained task and the tool returns its report — the user sees only your reply.
Pick the route from `scry` exactly as for `summon`; the same claim and effort rules
apply. Give the accomplice a short mnemonic `name`. The brief must stand alone: the
accomplice cannot see this chat and has no user to ask, so include the goal, the
relevant context, and what a finished result looks like. Several commissions in one
reply run concurrently.

### `whisper` — a quiet word to an accomplice
Continue an accomplice's work by `name`: it remembers everything it did. Use it to
refine or extend that work instead of commissioning a new accomplice.
"""


@dataclass
class GatewayCapability(AbstractCapability[None]):
    # What each channel can route to, keyed by channel id — not one list, because a
    # spell that crosses lands on a channel with its own idea of who runs there.
    # This run's own channel answers `routes`; the rest answer a crossing.
    channel_routes: dict[str, list[AgentRoute]]
    current_agent_id: str
    # Every connected channel, so `surfaces` can be read for any of them and not
    # just this run's own — what a cross-channel move needs.
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    # The identity registry and the run's own profile: together they say where else
    # this person is reachable. Both None on a gate built only to route locally.
    users: UserManager | None = None
    user_profile: UserProfile | None = None
    # What running an accomplice takes. All None on a gate built only to route,
    # which then does not offer the accomplice spells at all.
    agents: dict[str, AgentTentacle] | None = None
    conversations: ConversationManager | None = None
    thread_id: uuid.UUID | None = None
    # Where this run lives; also what `allow_here` and `private_blocked_by` read.
    conversation_address: ChannelAddress | None = None
    commission_timeout: float = COMMISSION_TIMEOUT
    commissioning: bool = field(default=False, init=False)
    decision: SummonDecision | SchemeDecision | None = field(default=None, init=False)
    # Every route on this run's own channel but the current agent's own — the info
    # shared with the agent to decide where to go, and what a spell landing here
    # validates a chosen route against. A crossing validates against its own.
    other_routes: list[AgentRoute] = field(init=False, repr=False)
    # `destinations` is computed once per gate, and a gate lasts one turn. Held here
    # rather than recomputed because resolving it reaches the identity registry.
    computed_destinations: list[Destination] | None = field(
        default=None, init=False, repr=False
    )
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

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
        # Nothing built from runtime state may reach a tool definition: they are a
        # provider prompt-cache breakpoint (`anthropic_cache_tool_definitions`) at the
        # front of the prefix, so a schema that varies forks it into variants that never
        # warm each other. Hence plain `str` routes, validated by `claimed_route`
        # against the list `scry` returns — a tool *result*, after the breakpoint.
        toolset: FunctionToolset[None] = FunctionToolset(id=GATEWAY_TOOLSET_ID)
        toolset.tool(name=SCRY_TOOL_NAME)(self.scry)
        toolset.tool(name=SUMMON_TOOL_NAME, retries=2)(self.summon)
        # `retries` to match its siblings: teleport refuses a surface with no
        # sub-thread to open, so it needs the same room to be told and correct.
        toolset.tool(name=TELEPORT_TOOL_NAME, retries=2)(self.teleport)
        toolset.tool(name=SCHEME_TOOL_NAME, retries=2)(self.scheme)
        toolset.tool(name=SEND_TOOL_NAME, retries=2)(self.send)
        if (
            self.agents is not None
            and self.conversations is not None
            and self.thread_id is not None
            and self.conversation_address is not None
        ):
            self.commissioning = True
            toolset.tool(name=COMMISSION_TOOL_NAME, retries=2)(self.commission)
            toolset.tool(name=WHISPER_TOOL_NAME, retries=2)(self.whisper)
        self.toolset = toolset

    def commission_deps(
        self,
    ) -> tuple[
        dict[str, AgentTentacle], ConversationManager, uuid.UUID, ChannelAddress
    ]:
        """The live handles the accomplice spells run with. Registration only
        offers the spells when all four are set, so a miss here is a
        construction bug, not a model mistake."""
        if (
            self.agents is None
            or self.conversations is None
            or self.thread_id is None
            or self.conversation_address is None
        ):
            raise RuntimeError(
                "the accomplice spells need agents, conversations, a thread and an address"
            )
        return (
            self.agents,
            self.conversations,
            self.thread_id,
            self.conversation_address,
        )

    @property
    def allow_here(self) -> bool:
        """Whether `summon here` may hand this conversation over in place.

        False on a group's main channel, where pinning an owner would route every
        gated-in message, from any user, to one agent."""
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

        True for a gate with no surface to judge by, as `allow_here` is: refusing
        what it cannot see would block a spell the graph resolves correctly anyway.
        """
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

        A gate that knows no channels can reach no direct messages."""
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
        the channel it is already on.
        """
        address = self.conversation_address
        if address is None:
            return []
        crossing: list[Destination] = []
        for one in await self.destinations():
            if one.address.channel_tentacle_id == address.channel_tentacle_id:
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
        """The place `handle` names, or a `ModelRetry` listing what it could have
        named. The model never names an address — this is where one comes from."""
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
        raise ModelRetry(
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
            raise ModelRetry(
                f"Invalid {spell} route (agent_id={agent_id!r}, "
                f"model={model!r}). Copy an agent_id and model exactly "
                f"from one of these routes:\n{available}"
            )
        if effort is not None and effort not in route.claim.efforts:
            raise ModelRetry(
                f"Route (agent_id={agent_id!r}, model={model!r}) does "
                f"not accept effort {effort!r}; it claims "
                f"{'/'.join(route.claim.efforts)}. Pick one of those, or omit "
                f"effort."
            )
        return route

    async def run_accomplice(
        self,
        ctx: RunContext[None],
        *,
        child: Conversation,
        run_name: str,
        prompt: str,
        model: Model | str | None,
        effort: ThinkingEffort | None,
    ) -> str:
        agents, conversations, thread_id, conversation_address = self.commission_deps()
        if ctx.run_id is None:
            raise RuntimeError("a commission needs the parent run id for the run tree")
        agent = agents[child.agent_tentacle_id]
        try:
            # The subagent contract (non-interactive, no capabilities,
            # addressed at the pre-ensured child conversation) lives on
            # `subagent_run`; the accomplice framing is this spawner's and is
            # passed here. An accomplice that defers anyway ends its run with
            # DeferredToolRequests, surfaced loudly below instead of parking a
            # batch nothing resumes.
            result = await asyncio.wait_for(
                agent.subagent_run(
                    prompt,
                    conversation_address=conversation_address,
                    thread_id=thread_id,
                    conversation_id=child.id,
                    run_name=run_name,
                    model=model,
                    effort=effort,
                    instructions=ACCOMPLICE_INSTRUCTION,
                ),
                timeout=self.commission_timeout,
            )
        except TimeoutError:
            raise ModelRetry(
                f"The accomplice {child.subagent_id!r} exceeded "
                f"{self.commission_timeout:.0f}s and was stopped. What it "
                f"recorded is kept — `{WHISPER_TOOL_NAME}` to continue "
                "it, or break the work into smaller briefs."
            ) from None
        # The runner recorded its turn without knowing its place;
        # the spawner stamps the run tree after the fact.
        await conversations.link_parent_run(
            result.run_id,
            parent_run_id=ctx.run_id,
            parent_tool_call_id=ctx.tool_call_id,
        )
        output = result.output
        if isinstance(output, DeferredToolRequests):
            raise ModelRetry(
                f"The accomplice {child.subagent_id!r} tried to ask the "
                "user or defer — an accomplice has no user. Write a "
                "self-contained brief that needs no clarification, then "
                "commission another."
            )
        if isinstance(output, str):
            return output
        if isinstance(output, Iterable):
            return "\n\n".join(str(part) for part in output)
        return str(output)

    async def scry(self, ctx: RunContext[None]) -> Scrying:
        """Reveal what this conversation can reach: the Octomate agent tentacles
        that can be summoned or commissioned, and anywhere other than here that the
        person you are answering can be reached privately."""
        return Scrying(routes=self.other_routes, destinations=await self.destinations())

    async def summon(
        self,
        ctx: RunContext[None],
        agent_id: str,
        model: str,
        destination: SummonTarget,
        hint: str,
        reason: str,
        summon: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Hand this conversation to another Octomate agent, who takes it over.

        Args:
            agent_id: The target agent, copied exactly from a `scry` route — from
                that destination's own routes when you name a channel, since which
                agents run where is each channel's own business.
            model: That route's model, copied exactly.
            destination: Where the other agent picks it up. A channel opens a
                sub-thread of that person's direct messages there.
            hint: A short, user-facing note announcing the handoff; used as the
                opener when a new thread is started.
            reason: One line on why this agent fits — recorded with the handoff, not
                shown to the user as the reply.
            summon: The self-contained brief the other agent starts from. It becomes
                their opening prompt and they cannot see this conversation, so give
                the goal, the relevant context and decisions, what's been tried, and
                what a finished result looks like.
            effort: How hard the agent should think, from the effort levels the
                route's claim offers. Set it only when the user explicitly asked
                for a level; omitted, the agent's own default applies.
        """
        handles = await self.summon_handles()
        if destination.handle not in handles:
            raise ModelRetry(
                self.no_landing(destination.handle, handles, spell="summon")
            )
        if agent_id == self.current_agent_id:
            raise ModelRetry(
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
        ctx: RunContext[None],
        hint: str,
        destination: TeleportTarget = THREAD_TARGET,
    ) -> str:
        """Continue this conversation yourself in a new sub-thread; everything said
        so far comes with you.

        Args:
            hint: The short, user-facing thread-starter message.
            destination: Where to carry it, a sub-thread of this chat by default. A
                channel takes it into their direct messages there, and is offered
                only out of a conversation nobody else can read — everything said
                here goes with you, and it is not all yours to move.
        """
        handles = await self.teleport_handles()
        if destination.handle not in handles:
            raise ModelRetry(
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
            raise ModelRetry(
                f"{channel.name} does not run you ({self.current_agent_id}), and a "
                f"teleport takes you with it. Carry on here, or `{SUMMON_TOOL_NAME}` "
                "an agent it does run."
            )
        # Two plain strings rather than the resolved address: the far end is always
        # somebody's direct messages, which is exactly what `open_dm` takes, and
        # metadata rides through the deferral untyped either way.
        raise CallDeferred(
            metadata={
                "kind": TELEPORT_DEFER_KIND,
                "hint": hint,
                "channel": crossing.address.channel_tentacle_id if crossing else "",
                "user": crossing.address.user_id if crossing else "",
            }
        )

    async def scheme(
        self,
        ctx: RunContext[None],
        hint: str,
        brief: str,
        destination: SchemeTarget = DIRECT_TARGET,
    ) -> str:
        """Continue this with the user one-to-one, in their direct messages.

        Whoever already handles that user's direct messages picks the work up — you do
        not choose them, and their direct messages keep the owner they had. Use it for
        work that is that person's alone, or that does not belong in front of the group.

        Args:
            hint: The short, user-facing line that opens the conversation over there,
                where they will read it before anything else. Say that the work is
                being picked up — not the brief, which is written for whoever answers.
            brief: The self-contained brief whoever answers there starts from. They
                cannot see this conversation, so give the goal, the relevant context and
                decisions, what's been tried, and what a finished result looks like.
            destination: Whose direct messages — this channel's by default, or a
                channel from `scry` to continue where they already are.
        """
        where = await self.destination(destination.handle, spell="scheme")
        self.decision = SchemeDecision(
            hint=hint, brief=brief, destination=where.address
        )
        return f"Taking this to {where.label}."

    async def send(
        self,
        ctx: RunContext[None],
        segments: list[MessageSegment],
        destination: SendTarget = HERE_TARGET,
    ) -> ToolReturn[str]:
        """Deliver these segments immediately, without ending your turn — a progress
        update, an intermediate result, an image or a file. What you send here is
        already delivered: do NOT repeat it in your final reply.

        Args:
            segments: What to deliver.
            destination: Where to deliver it, this conversation by default. Say in
                your reply when you sent it somewhere other than here.
        """
        # Being in their direct messages already stops a `scheme` — nowhere to move
        # the conversation to — but never a send: that *is* where it was asked to go,
        # so it lands here rather than being refused.
        already_there = (
            destination.handle == DIRECT_TARGET.handle
            and self.private_blocked_by == "already_private"
        )
        address = (
            None
            if destination.handle == HERE_TARGET.handle or already_there
            else (await self.destination(destination.handle, spell="send")).address
        )
        return ToolReturn(
            return_value="sent",
            metadata=[MessageSentEvent(segments=segments, destination=address)],
        )

    async def commission(
        self,
        ctx: RunContext[None],
        name: str,
        agent_id: str,
        model: str,
        brief: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Draw another Octomate agent into working a task and get its report
        back — the user sees none of it.

        Args:
            name: Your name for this accomplice — short and mnemonic, e.g.
                `repo-audit`. `whisper` to it later to follow up.
            agent_id: The agent to draw in, copied exactly from a
                `scry` route.
            model: That route's model, copied exactly.
            brief: The self-contained work order. The accomplice cannot see
                this conversation and has no user to ask, so give the
                goal, the relevant context and decisions, and what a
                finished result looks like.
            effort: How hard the accomplice should think, from the levels the
                route's claim offers. Set it only when the user
                explicitly asked for a level.
        """
        agents, conversations, thread_id, _ = self.commission_deps()
        if not name.strip():
            raise ModelRetry("Give the accomplice a short, mnemonic name.")
        if agent_id == self.current_agent_id:
            raise ModelRetry(
                f"Cannot commission yourself {self.current_agent_id!r}. "
                f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
            )
        route = self.claimed_route(agent_id, model, effort, spell="commission")
        run_model = agents[agent_id].models.get(route.model)
        if run_model is None:
            raise ModelRetry(
                f"Agent {agent_id!r} does not serve model {model!r}. "
                f"Call `{SCRY_TOOL_NAME}` and copy a route exactly."
            )
        # The calling run's own conversation is the parent — the react
        # graph put its id on the RunContext. No id means the gate is
        # mounted outside a live run: raise, never conjure a parent.
        if ctx.conversation_id is None:
            raise RuntimeError("a commission needs the calling run's conversation id")
        parent_id = uuid.UUID(ctx.conversation_id)
        child = await conversations.ensure(
            thread_id,
            agent_tentacle_id=agent_id,
            subagent_id=name,
            parent_conversation_id=parent_id,
        )
        if child.parent_conversation_id != parent_id or child.runs:
            raise ModelRetry(
                f"{name!r} is already at work — `{WHISPER_TOOL_NAME}` "
                "to it, or pick a new name."
            )
        return await self.run_accomplice(
            ctx,
            child=child,
            run_name="commission",
            prompt=brief,
            model=run_model,
            effort=effort,
        )

    async def whisper(self, ctx: RunContext[None], name: str, message: str) -> str:
        """A quiet word to an accomplice you commissioned. It keeps
        everything it did — your whisper continues its work, not a fresh
        start.

        Args:
            name: The name you gave the accomplice when you commissioned it.
            message: The follow-up work order — self-contained, like a
                brief; the accomplice still cannot see this conversation.
        """
        _, conversations, thread_id, _ = self.commission_deps()
        if ctx.conversation_id is None:
            raise RuntimeError("a whisper needs the calling run's conversation id")
        parent_id = uuid.UUID(ctx.conversation_id)
        hands = await conversations.subagents(parent_id)
        stored = next((hand for hand in hands if hand.subagent_id == name), None)
        if stored is None:
            live = ", ".join(sorted(hand.subagent_id for hand in hands))
            raise ModelRetry(
                f"No accomplice named {name!r}. "
                + (
                    f"Accomplices at work: {live}."
                    if live
                    else "No accomplices are at work."
                )
                + f" `{COMMISSION_TOOL_NAME}` one to start."
            )
        child = await conversations.ensure(
            thread_id,
            agent_tentacle_id=stored.agent_tentacle_id,
            subagent_id=name,
            parent_conversation_id=parent_id,
        )
        return await self.run_accomplice(
            ctx,
            child=child,
            run_name="whisper",
            prompt=message,
            model=None,
            effort=None,
        )

    def get_instructions(self) -> str:
        if self.commissioning:
            return GATE_INSTRUCTION + SEND_INSTRUCTION + COMMISSION_INSTRUCTION
        return GATE_INSTRUCTION + SEND_INSTRUCTION

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[None],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        """Put each `send`'s stashed `MessageSentEvent` on the run stream,
        where the consumer rendering this run delivers it."""
        async for event in stream:
            yield event
            if (
                isinstance(event, FunctionToolResultEvent)
                and isinstance(event.part, ToolReturnPart)
                and event.part.tool_name == SEND_TOOL_NAME
                and isinstance(event.part.metadata, list)
            ):
                for sent_event in event.part.metadata:
                    # One dynamic-boundary cast: pydantic-ai types the stream as
                    # AgentStreamEvent, consumers match the concrete octomate event.
                    yield cast(AgentStreamEvent, sent_event)

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
