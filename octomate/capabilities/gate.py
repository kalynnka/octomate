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

from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models import KnownModelName
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.config.agents import ClaudeCodeModelName
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

In plain terms, these tools route the conversation. If you can just answer, do that
and call none of them. Otherwise:

- `scry`: list the other agents you can hand off to (their names and what each is good
  at). Call it first to choose a valid `summon` target.
- `summon`: hand this conversation to another agent. They become its owner and continue
  from a self-contained brief you write. Use when a different agent is clearly better
  suited. Copy `agent_id` and `model` exactly from one `scry` route, and choose
  `destination`: `here` hands it over in this same conversation, `thread` hands it off
  into a new sub-thread of the current chat.
- `teleport`: move this conversation into a new sub-thread of the current chat and keep
  handling it yourself; everything said so far comes with you. Use for multi-step or
  long-running work that deserves its own thread.

The current agent is not a valid summon target.
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
        toolset: FunctionToolset[None] = FunctionToolset(id=GATE_TOOLSET_ID)

        @toolset.tool(name=SCRY_TOOL_NAME)
        async def scry(ctx: RunContext[None]) -> list[SummonRoute]:
            """Reveal the Octomate agent tentacles that can be summoned from you."""
            return self.summonable_routes

        @toolset.tool(name=SUMMON_TOOL_NAME)
        async def summon(
            ctx: RunContext[None],
            agent_id: str,
            model: KnownModelName | ClaudeCodeModelName,
            destination: SummonDestination,
            hint: str,
            reason: str,
            summon: str,
        ) -> str:
            """Ask another Octomate agent to continue this request."""
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
                raise ModelRetry(
                    "Invalid summon route "
                    f"(agent_id={agent_id!r}, model={decision.model!r}). "
                    f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
                )
            self.decision = decision
            return f"Summoning {agent_id} ({decision.model}) → {destination}."

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
