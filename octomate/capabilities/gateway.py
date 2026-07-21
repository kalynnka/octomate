"""Gate capability: an agent's routing spellbook.

Five spells decide where a turn goes and who handles it. Each is opaque on its own,
so the instruction opens with plain words for what they actually do:

- `scry`: reveal the other agents this one can hand off to or put to work.
- `summon`: hand the conversation to another agent (a handoff — they take over from a
  brief). The graph reads the recorded decision after the run.
- `teleport`: continue the same agent in a new place (a sub-thread), carrying the
  history forward. Deferred, so the graph can fork the history and resume.
- `scheme`: draw another agent into working a self-contained task in the background
  and return its report — an ordinary awaited tool call, never a deferral; the caller
  keeps the conversation and the user sees none of it.
- `whisper`: a quiet follow-up to an accomplice by name; it keeps its own context.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, Literal

from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.config.agents import AgentRouteModelName
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.triage import (
    AgentRoute,
    AgentRouteKey,
    SummonDecision,
    SummonDestination,
)

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from octomate.managers.conversation import ConversationManager
    from octomate.tentacles.agent.base import AgentTentacle

SCRY_TOOL_NAME = "scry"
SUMMON_TOOL_NAME = "summon"
TELEPORT_TOOL_NAME = "teleport"
SCHEME_TOOL_NAME = "scheme"
WHISPER_TOOL_NAME = "whisper"
# A scheme holds the parent's live tool call open while the accomplice runs, so the
# wait must not be unbounded (`approval_timeout` is the precedent). Seconds.
SCHEME_TIMEOUT = 900.0
# The `teleport` deferral's declared metadata kind. The suspender and dispatch graph
# classify the deferral by this kind rather than the tool name, so `gate` (which emits
# it) and `reflex` (which resolves it) agree on one value without matching on the name.
TELEPORT_KIND = "teleport"
GATE_TOOLSET_ID = "gate"

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
sub-thread of the current chat. You yourself are not a valid summon target.

### `teleport` — relocate yourself
Move this conversation into a new sub-thread that *you* keep handling, carrying everything
said so far. Use it for multi-step or long-running work that deserves its own thread but
that you are the right one to do — no other agent involved.
"""

SCHEME_INSTRUCTION = """\

### `scheme` — put another agent to work in the background (you keep the conversation)
Where `summon` hands the conversation away, `scheme` does not: another agent works a
self-contained task and the tool returns its report — the user sees only your reply.
Pick the route from `scry` exactly as for `summon`; the same claim and effort rules
apply. Give the accomplice a short mnemonic `name`. The brief must stand alone: the
accomplice cannot see this chat and has no user to ask, so include the goal, the
relevant context, and what a finished result looks like. Several schemes in one
reply run concurrently.

### `whisper` — a quiet word to an accomplice
Continue an accomplice's work by `name`: it remembers everything it did. Use it to
refine or extend that work instead of scheming a new accomplice.
"""

# An accomplice carries no gate at all — no summon, no teleport, no schemes of
# its own. This rides the accomplice's run as plain instructions instead.
ACCOMPLICE_INSTRUCTION = """\
You are an accomplice: another agent drew you into its scheme, and your reply is
your report back to it. There is no user here to ask, and anything that needs a human
approval is declined immediately — work from the brief you were given within what
you can do unaided; if something is under-specified or unapprovable, state the
assumption or the blocker in your report and proceed.
"""


def narrowed(method: MethodType, **annotations: object) -> MethodType:
    """A per-instance copy of a bound method whose signature carries narrowed
    annotations — the live-route `Literal`s. Stamping the method's own
    `__annotations__` would edit the class-shared function, and every gate
    instance has different routes, so each stamps a clone of its own."""
    func = method.__func__
    clone = FunctionType(
        func.__code__,
        func.__globals__,
        func.__name__,
        func.__defaults__,
        func.__closure__,
    )
    clone.__kwdefaults__ = func.__kwdefaults__
    clone.__doc__ = func.__doc__
    clone.__annotations__ = {**func.__annotations__, **annotations}
    return MethodType(clone, method.__self__)


@dataclass
class GatewayCapability(AbstractCapability[None]):
    routes: list[AgentRoute]
    current_agent_id: str
    # False on a group main channel, where pinning an owner would route every
    # gated-in message (from any user) to one agent — so `summon here` is refused there.
    allow_here: bool = True
    # What the scheme spells need to actually run an accomplice: the connected
    # agents, the conversation manager, and where the run lives. All None on a
    # gate built only to route (summon/teleport surfaces, tests) — the
    # scheme spells are then not offered at all.
    agents: dict[str, AgentTentacle] | None = None
    conversations: ConversationManager | None = None
    thread_id: uuid.UUID | None = None
    conversation_address: ChannelAddress | None = None
    scheme_timeout: float = SCHEME_TIMEOUT
    scheming: bool = field(default=False, init=False)
    decision: SummonDecision | None = field(default=None, init=False)
    # Every route but the current agent's own — the info shared with the agent to
    # decide where to go: what `scry` reveals, and what every spell validates a
    # chosen route against.
    other_routes: list[AgentRoute] = field(init=False, repr=False)
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.other_routes = [
            route for route in self.routes if route.agent_id != self.current_agent_id
        ]
        # Constrain the spell args to the routes actually on offer. Left as their
        # declared types, pydantic-ai renders `model`'s full union — the ~500-entry
        # KnownModelName literal — into the tool schema, drowning the real routes and
        # tempting a weak entry model to pick a plausible-but-unrouteable id. Build a
        # `Literal` of the live route values at runtime and stamp it on the tool's
        # signature so the model only ever sees the handful that route.
        route_agent_ids = tuple(
            dict.fromkeys(route.agent_id for route in self.other_routes)
        )
        route_models = tuple(
            dict.fromkeys(str(route.model) for route in self.other_routes)
        )
        agent_id_type = Literal[route_agent_ids] if route_agent_ids else str
        model_type = Literal[route_models] if route_models else str
        toolset: FunctionToolset[None] = FunctionToolset(id=GATE_TOOLSET_ID)
        toolset.tool(name=SCRY_TOOL_NAME)(self.scry)
        toolset.tool(name=SUMMON_TOOL_NAME, retries=2)(
            narrowed(self.summon, agent_id=agent_id_type, model=model_type)
        )
        toolset.tool(name=TELEPORT_TOOL_NAME)(self.teleport)
        if (
            self.agents is not None
            and self.conversations is not None
            and self.thread_id is not None
            and self.conversation_address is not None
        ):
            self.scheming = True
            toolset.tool(name=SCHEME_TOOL_NAME, retries=2)(
                narrowed(self.scheme, agent_id=agent_id_type, model=model_type)
            )
            toolset.tool(name=WHISPER_TOOL_NAME, retries=2)(self.whisper)
        self.toolset = toolset

    def scheme_deps(
        self,
    ) -> tuple[
        dict[str, AgentTentacle], ConversationManager, uuid.UUID, ChannelAddress
    ]:
        """The live handles the scheme spells run with. Registration only
        offers the spells when all four are set, so a miss here is a
        construction bug, not a model mistake."""
        if (
            self.agents is None
            or self.conversations is None
            or self.thread_id is None
            or self.conversation_address is None
        ):
            raise RuntimeError(
                "the scheme spells need agents, conversations, a thread and "
                "an address"
            )
        return (
            self.agents,
            self.conversations,
            self.thread_id,
            self.conversation_address,
        )

    def claimed_route(
        self,
        agent_id: str,
        model: AgentRouteModelName,
        effort: ThinkingEffort | None,
        *,
        spell: str,
    ) -> AgentRoute:
        """The offered route for (agent_id, model), with the requested effort
        validated against its claim — the shared gatekeeping of `summon` and
        `scheme`."""
        key = AgentRouteKey(agent_id=agent_id, model=model)
        route = next((route for route in self.other_routes if route.key == key), None)
        if route is None:
            available = "\n".join(str(route) for route in self.other_routes)
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
        agents, conversations, thread_id, conversation_address = self.scheme_deps()
        if ctx.run_id is None:
            raise RuntimeError("a scheme needs the parent run id for the run tree")
        agent = agents[child.agent_tentacle_id]
        try:
            # No deferred_suspender, no resolver, and no capabilities: a
            # accomplice gets no gate at all — nested scheming does not
            # exist, and one that defers anyway ends its run with
            # DeferredToolRequests, surfaced loudly below instead of
            # parking a batch nothing resumes. The agent gets only an
            # address — the pre-ensured child conversation — and stays
            # ignorant of why it runs.
            result = await asyncio.wait_for(
                agent.run(
                    prompt,
                    conversation_address=conversation_address,
                    thread_id=thread_id,
                    run_name=run_name,
                    model=model,
                    effort=effort,
                    conversation_id=child.id,
                    interactive=False,
                    instructions=ACCOMPLICE_INSTRUCTION,
                ),
                timeout=self.scheme_timeout,
            )
        except asyncio.TimeoutError:
            raise ModelRetry(
                f"The accomplice {child.subagent_id!r} exceeded "
                f"{self.scheme_timeout:.0f}s and was stopped. What it "
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
                "scheme again."
            )
        if isinstance(output, str):
            return output
        if isinstance(output, Iterable):
            return "\n\n".join(str(part) for part in output)
        return str(output)

    async def scry(self, ctx: RunContext[None]) -> list[AgentRoute]:
        """Reveal the Octomate agent tentacles that can be summoned from you."""
        return self.other_routes

    async def summon(
        self,
        ctx: RunContext[None],
        agent_id: str,
        model: AgentRouteModelName,
        destination: SummonDestination,
        hint: str,
        reason: str,
        summon: str,
        effort: ThinkingEffort | None = None,
    ) -> str:
        """Hand this conversation to another Octomate agent, who takes it over.

        Args:
            agent_id: The target agent, copied exactly from a `scry` route.
            model: That route's model, copied exactly.
            destination: `here` to hand over this same conversation, or `thread` to
                open a new sub-thread of the current chat and hand off there.
            hint: A short, user-facing note announcing the handoff; used as the
                opener when a new `thread` is started.
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
        if destination == "here" and not self.allow_here:
            raise ModelRetry(
                "Cannot take over a group's main channel in place. "
                "Summon into a `thread` instead."
            )
        decision = SummonDecision(
            action="summon",
            agent_id=agent_id,
            model=model,
            destination=destination,
            effort=effort,
            hint=hint,
            reason=reason,
            summon=summon,
        )
        if agent_id == self.current_agent_id:
            raise ModelRetry(
                f"Cannot summon yourself {self.current_agent_id!r}. "
                f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
            )
        self.claimed_route(agent_id, decision.model, effort, spell="summon")
        self.decision = decision
        return f"Summoning {agent_id} ({decision.model}) → {destination}."

    async def teleport(self, ctx: RunContext[None], hint: str) -> str:
        """Continue this conversation yourself in a new sub-thread of the current
        chat; everything said so far comes with you. `hint` is the short
        user-facing thread-starter message."""
        raise CallDeferred(metadata={"kind": TELEPORT_KIND, "hint": hint})

    async def scheme(
        self,
        ctx: RunContext[None],
        name: str,
        agent_id: str,
        model: AgentRouteModelName,
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
        agents, conversations, thread_id, _ = self.scheme_deps()
        if not name.strip():
            raise ModelRetry("Give the accomplice a short, mnemonic name.")
        if agent_id == self.current_agent_id:
            raise ModelRetry(
                f"Cannot scheme with yourself {self.current_agent_id!r}. "
                f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
            )
        self.claimed_route(agent_id, model, effort, spell="scheme")
        run_model = agents[agent_id].models.get(model)
        if run_model is None:
            raise ModelRetry(
                f"Agent {agent_id!r} does not serve model {model!r}. "
                f"Call `{SCRY_TOOL_NAME}` and copy a route exactly."
            )
        # The calling run's own conversation is the parent — the react
        # graph put its id on the RunContext. No id means the gate is
        # mounted outside a live run: raise, never conjure a parent.
        if ctx.conversation_id is None:
            raise RuntimeError("a scheme needs the calling run's conversation id")
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
            run_name="scheme",
            prompt=brief,
            model=run_model,
            effort=effort,
        )

    async def whisper(self, ctx: RunContext[None], name: str, message: str) -> str:
        """A quiet word to an accomplice you schemed with. It keeps
        everything it did — your whisper continues its work, not a fresh
        start.

        Args:
            name: The name you gave the accomplice when you schemed.
            message: The follow-up work order — self-contained, like a
                brief; the accomplice still cannot see this conversation.
        """
        _, conversations, thread_id, _ = self.scheme_deps()
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
                + f" `{SCHEME_TOOL_NAME}` one to start."
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
        if self.scheming:
            return GATE_INSTRUCTION + SCHEME_INSTRUCTION
        return GATE_INSTRUCTION

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
