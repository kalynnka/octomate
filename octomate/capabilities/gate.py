"""Gate capability: an agent's routing spellbook.

Three spells decide where a turn goes and who handles it. Each is opaque on its own,
so the instruction opens with plain words for what they actually do:

- `scry`: reveal the other agents this one can hand off to.
- `summon`: hand the conversation to another agent (a handoff — they take over from a
  brief). The graph reads the recorded decision after the run.
- `teleport`: continue the same agent in a new place (a sub-thread), carrying the
  history forward. Deferred, so the graph can fork the history and resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.config.agents import AgentRouteModelName
from octomate.schemas.triage import (
    SummonDecision,
    SummonDestination,
    SummonRoute,
    SummonRouteKey,
)

SCRY_TOOL_NAME = "scry"
SUMMON_TOOL_NAME = "summon"
TELEPORT_TOOL_NAME = "teleport"
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

When one fires, call `scry` first to see the agents and what each is for, pick the one
whose description clearly matches, and `summon` it — copying its `agent_id` and `model`
exactly from that route, and writing a self-contained brief since the other agent may not
see this chat. Choose `destination`: `here` hands over this same conversation; `thread`
opens a new sub-thread of the current chat. You yourself are not a valid summon target.

### `teleport` — relocate yourself
Move this conversation into a new sub-thread that *you* keep handling, carrying everything
said so far. Use it for multi-step or long-running work that deserves its own thread but
that you are the right one to do — no other agent involved.
"""


@dataclass
class GateCapability(AbstractCapability[None]):
    routes: list[SummonRoute]
    current_agent_id: str
    # False on a group main channel, where pinning an owner would route every
    # gated-in message (from any user) to one agent — so `summon here` is refused there.
    allow_here: bool = True
    decision: SummonDecision | None = field(default=None, init=False)
    summonable_routes: list[SummonRoute] = field(init=False, repr=False)
    route_keys: set[SummonRouteKey] = field(init=False, repr=False)
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.summonable_routes = [
            route for route in self.routes if route.agent_id != self.current_agent_id
        ]
        self.route_keys = {route.key for route in self.summonable_routes}
        # Constrain the summon args to the routes actually on offer. Left as their
        # declared types, pydantic-ai renders `model`'s full union — the ~500-entry
        # KnownModelName literal — into the tool schema, drowning the real routes and
        # tempting a weak entry model to pick a plausible-but-unrouteable id. Build a
        # `Literal` of the live route values at runtime and stamp it on the tool's
        # signature so the model only ever sees the handful that route.
        route_agent_ids = tuple(
            dict.fromkeys(route.agent_id for route in self.summonable_routes)
        )
        route_models = tuple(
            dict.fromkeys(str(route.model) for route in self.summonable_routes)
        )
        agent_id_type = Literal[route_agent_ids] if route_agent_ids else str
        model_type = Literal[route_models] if route_models else str
        toolset: FunctionToolset[None] = FunctionToolset(id=GATE_TOOLSET_ID)

        @toolset.tool(name=SCRY_TOOL_NAME)
        async def scry(ctx: RunContext[None]) -> list[SummonRoute]:
            """Reveal the Octomate agent tentacles that can be summoned from you."""
            return self.summonable_routes

        async def summon(
            ctx: RunContext[None],
            agent_id: str,
            model: AgentRouteModelName,
            destination: SummonDestination,
            hint: str,
            reason: str,
            summon: str,
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
                hint=hint,
                reason=reason,
                summon=summon,
            )
            if agent_id == self.current_agent_id:
                raise ModelRetry(
                    f"Cannot summon yourself {self.current_agent_id!r}. "
                    f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
                )
            if decision.key not in self.route_keys:
                available = "\n".join(str(route) for route in self.summonable_routes)
                raise ModelRetry(
                    f"Invalid summon route (agent_id={agent_id!r}, "
                    f"model={decision.model!r}). Copy an agent_id and model exactly "
                    f"from one of these routes:\n{available}"
                )
            self.decision = decision
            return f"Summoning {agent_id} ({decision.model}) → {destination}."

        # Stamp the runtime `Literal`s onto the signature before registration so the
        # generated tool schema offers only the live routes, not every known model.
        summon.__annotations__["agent_id"] = agent_id_type
        summon.__annotations__["model"] = model_type
        toolset.tool(name=SUMMON_TOOL_NAME, retries=3)(summon)

        @toolset.tool(name=TELEPORT_TOOL_NAME)
        async def teleport(ctx: RunContext[None], hint: str) -> str:
            """Continue this conversation yourself in a new sub-thread of the current
            chat; everything said so far comes with you. `hint` is the short
            user-facing thread-starter message."""
            raise CallDeferred(metadata={"kind": TELEPORT_KIND, "hint": hint})

        self.toolset = toolset

    def get_instructions(self) -> str:
        return GATE_INSTRUCTION

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
