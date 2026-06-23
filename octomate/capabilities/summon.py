"""Summon capability: optionally choose a validated agent route.

The graph owns dispatch. This capability gives an agent run the current summon
route catalog, then exposes a tool that records the selected summon for the
graph to dispatch after the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.schemas.triage import SummonDecision, SummonRoute

SUMMON_TOOL_NAME = "summon"
SUMMON_INSTRUCTION = """\
## Summon

You have a `summon` tool. Use it when another agent should continue this request
instead of answering directly. If you can answer directly, do that without
calling `summon`.

When you call `summon`, provide:
- `agent_id` and `model` copied exactly from one available summon route.
- `hint`: a short user-facing thread starter.
- `reason`: why this route is appropriate.
- `summon`: a detailed, self-contained brief for the next agent.

The current agent `{current_agent_id}` is not a valid summon target.

Available summon routes:
{routes}
"""


@dataclass
class SummonCapability(AbstractCapability[None]):
    routes: list[SummonRoute]
    current_agent_id: str
    decision: SummonDecision | None = field(default=None, init=False)
    route_keys: set[tuple[str, str]] = field(init=False, repr=False)
    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.route_keys = {route.key for route in self.routes}
        toolset: FunctionToolset[None] = FunctionToolset(id="summon")

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
            route_hint = "; ".join(
                f"agent_id={route.agent_id!r}, model={route.model!r}"
                for route in self.routes
            )
            if agent_id == self.current_agent_id:
                raise ModelRetry(
                    f"Cannot summon current agent {self.current_agent_id!r}. "
                    f"Choose one of: {route_hint}."
                )
            if decision.key not in self.route_keys:
                raise ModelRetry(
                    "Invalid summon route "
                    f"(agent_id={agent_id!r}, model={model!r}). "
                    f"Choose one of: {route_hint}."
                )
            self.decision = decision
            route_model = model or "default"
            return f"Summoning {agent_id} ({route_model})."

        self.toolset = toolset

    def get_instructions(self) -> AgentInstructions[None]:
        routes = "\n".join(str(route) for route in self.routes)
        return SUMMON_INSTRUCTION.format(
            current_agent_id=self.current_agent_id,
            routes=routes,
        )

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
