"""Summon capability: optionally choose a validated agent route.

The graph owns dispatch. This capability exposes one tool that reveals the
current summon route catalog, and another that records the selected summon for
the graph to dispatch after the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.schemas.triage import SummonDecision, SummonRoute

SCRY_TOOL_NAME = "scry"
SUMMON_TOOL_NAME = "summon"
SUMMON_INSTRUCTION = """\
## Summon

You have two tools:
- `scry`: reveal the currently summon routes to available agent tentacles.
- `summon`: ask another Octomate agent tentacle to handover this conversation.

If you can answer directly, do that without calling either tool. When another
agent should continue, call `scry`, choose one returned route, then call
`summon`.

When you call `summon`, provide:
- `agent_id` and `model` copied exactly from one route returned by `scry`.
- `hint`: a short user-facing thread starter.
- `reason`: why this route is appropriate.
- `summon`: a detailed, self-contained brief for the next agent.

The current agent is not a valid summon target.
"""


@dataclass
class SummonCapability(AbstractCapability[None]):
    routes: list[SummonRoute]
    current_agent_id: str
    decision: SummonDecision | None = field(default=None, init=False)
    summonable_routes: list[SummonRoute] = field(init=False, repr=False)
    route_keys: set[tuple[str, str]] = field(init=False, repr=False)
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.summonable_routes = [
            route for route in self.routes if route.agent_id != self.current_agent_id
        ]
        self.route_keys = {route.key for route in self.summonable_routes}
        toolset: FunctionToolset[None] = FunctionToolset(id="summon")

        @toolset.tool(name=SCRY_TOOL_NAME)
        async def scry(ctx: RunContext[None]) -> list[SummonRoute]:
            """Reveal the Octomate agents tentacles that can be summoned from you."""
            return self.summonable_routes

        @toolset.tool(name=SUMMON_TOOL_NAME)
        async def summon(
            ctx: RunContext[None],
            agent_id: str,
            model: str,
            hint: str,
            reason: str,
            summon: str,
        ) -> str:
            """Ask another Octomate agent to continue this request."""
            decision = SummonDecision(
                action="summon",
                agent_id=agent_id,
                model=model,
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
                    f"(agent_id={agent_id!r}, model={model!r}). "
                    f"Call `{SCRY_TOOL_NAME}` to choose a valid route."
                )
            self.decision = decision
            route_model = model or "default"
            return f"Summoning {agent_id} ({route_model})."

        self.toolset = toolset

    def get_instructions(self) -> str:
        return SUMMON_INSTRUCTION

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
