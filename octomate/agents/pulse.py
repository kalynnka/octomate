"""Pulse — lightweight internal planning for medium-complexity tasks.

Uses pydantic-ai's graph to triage requests, decompose eligible ones into
a sequence of steps backed by Todo items, execute each step via Agent.run(),
and return only the final synthesis.  Simple questions are answered directly
without planning.

The agent's configured output type and system prompt are respected throughout:
Triage and Synthesize return in the agent's native format (e.g. ``list[AgentMessage]``),
preserving personality and toolsets.  Only intermediate ExecuteStep calls use
``output_type=str`` since their outputs are internal.

Usage::

    answer = await run_pulse(agent, deps, "Summarize, compare, recommend")
    # *answer* matches the agent's native output type — no plan artifacts exposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


TRIAGE_INSTRUCTION = (
    "If the request below is a simple question, answer it directly.\n"
    "If it requires multiple steps, produce a structured plan of 2-5 Todo "
    "items, each with a todo_id (like 'pulse-0'), a short title, and a "
    "detailed description with instructions for that step."
)

STEP_INSTRUCTION = (
    "Complete ONLY the step described below and return ONLY the result. "
    "Do not mention the plan, other steps, or any meta-commentary.\n\n"
    "Overall goal: {goal}\n"
    "Previous context:\n{context}\n\n"
    "Current step: {title}\n"
    "Instructions: {description}"
)

SYNTHESIZE_INSTRUCTION = (
    "Using the collected results below, write a single coherent response that "
    "directly answers the original request. Do not mention steps, plans, or "
    "any internal process.\n\n"
    "Step results:\n{results}"
)


@dataclass
class PulseState:
    goal: str
    todos: list[Todo] = field(default_factory=list)
    step_outputs: list[str] = field(default_factory=list)


@dataclass
class PulseDeps:
    agent: Agent[Any, Any]
    agent_deps: Any
    message_history: list[ModelMessage] | None = None


def _build_triage_output_type(agent: Agent[Any, Any]) -> list[type]:
    """Combine the agent's native output types with ``list[Todo]`` for triage."""
    native = agent._output_type  # noqa: SLF001
    types: list[type] = list(native) if isinstance(native, list) else [native]
    types.append(list[Todo])  # type: ignore[arg-type]
    return types


def _is_todo_plan(output: Any) -> bool:
    return isinstance(output, list) and bool(output) and isinstance(output[0], Todo)


@dataclass
class Triage(BaseNode[PulseState, PulseDeps, Any]):
    """Classify the request and either answer directly or decompose into steps.

    Uses the agent's native output types combined with ``list[Todo]`` so the
    LLM can either respond in its configured format (direct answer, preserving
    personality) or produce a structured Todo plan.  Simple questions stay at
    1 LLM call.
    """

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | End[Any]:
        triage_output_type = _build_triage_output_type(ctx.deps.agent)
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            output_type=triage_output_type,
            instructions=TRIAGE_INSTRUCTION,
        )
        if not _is_todo_plan(result.output):
            return End(result.output)

        ctx.state.todos = result.output
        return ExecuteStep()


@dataclass
class ExecuteStep(BaseNode[PulseState, PulseDeps, Any]):
    """Execute the next pending Todo step."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | Synthesize:
        current = next(
            (t for t in ctx.state.todos if t.status == "pending"), None
        )
        if current is None:
            return Synthesize()

        context = "\n".join(ctx.state.step_outputs) if ctx.state.step_outputs else "(none)"
        step_instructions = STEP_INSTRUCTION.format(
            goal=ctx.state.goal,
            context=context,
            title=current.title,
            description=current.description,
        )
        result = await ctx.deps.agent.run(
            current.title,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            output_type=str,
            instructions=step_instructions,
        )
        ctx.state.step_outputs.append(f"- {current.title}: {result.output}")

        ctx.state.todos = [
            t.model_copy(update={"status": "done"})
            if t.todo_id == current.todo_id
            else t
            for t in ctx.state.todos
        ]
        return ExecuteStep()


@dataclass
class Synthesize(BaseNode[PulseState, PulseDeps, Any]):
    """Produce the final user-facing answer from all step outputs.

    Does NOT override ``output_type`` so the agent's configured format
    (e.g. ``list[AgentMessage]``) and personality are preserved.
    """

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> End[Any]:
        results_block = "\n".join(ctx.state.step_outputs)
        synth_instructions = SYNTHESIZE_INSTRUCTION.format(results=results_block)
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=synth_instructions,
        )
        return End(result.output)


pulse_graph = Graph(nodes=[Triage, ExecuteStep, Synthesize])


async def run_pulse(
    agent: Agent[Any, Any],
    deps: Any,
    prompt: str | list[str],
    *,
    message_history: list[ModelMessage] | None = None,
) -> Any:
    """Run the pulse pipeline: triage → optionally execute steps → synthesize.

    Simple questions are answered directly in a single call.  Complex multi-step
    requests are decomposed into Todo-backed steps and executed sequentially.

    Returns the agent's native output type — the same format the agent would
    produce without pulse (e.g. ``list[AgentMessage]``).
    """
    goal = prompt if isinstance(prompt, str) else " ".join(str(p) for p in prompt)
    state = PulseState(goal=goal)
    pulse_deps = PulseDeps(
        agent=agent, agent_deps=deps, message_history=message_history
    )
    result = await pulse_graph.run(Triage(), state=state, deps=pulse_deps)
    return result.output
