"""Pulse — lightweight internal planning for medium-complexity tasks.

Uses pydantic-ai's graph to classify requests, decompose eligible ones into
a sequence of steps backed by Todo items, execute each step via Agent.run(),
and return only the final synthesis.  Simple questions are answered directly
without planning.

Usage::

    answer = await run_pulse(agent, deps, "Summarize, compare, recommend")
    # *answer* is a clean, user-facing string — no plan artefacts exposed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from octomate.transmuters.interactions import Todo

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


CLASSIFY_INSTRUCTION = (
    "Decide whether the following request is a simple question that can be "
    "answered directly in one response, or a complex multi-step task that "
    "should be broken into smaller steps. Respond with ONLY one word: "
    "'direct' or 'plan'."
)

DECOMPOSE_INSTRUCTION = (
    "You are a silent planner. Given the user request below, break it into "
    "a short sequence of concrete steps (2-5). Each step should be a single, "
    "actionable instruction. Respond ONLY with the steps as a numbered list — "
    "no commentary, no headers. Example:\n"
    "1. Summarize the key points of X\n"
    "2. Compare them against Y\n"
    "3. Synthesize a final recommendation\n"
)

STEP_INSTRUCTION = (
    "You are executing one step of an internal plan. "
    "Complete ONLY the step described below and return ONLY the result. "
    "Do not mention the plan, other steps, or any meta-commentary.\n\n"
    "Overall goal: {goal}\n"
    "Previous context:\n{context}\n\n"
    "Current step: {instruction}"
)

SYNTHESIZE_INSTRUCTION = (
    "You are producing the final answer for the user. "
    "Using the step results below, write a single coherent response that directly "
    "answers the original request. Do NOT mention steps, plans, or any internal "
    "process.\n\n"
    "Original request: {goal}\n\n"
    "Step results:\n{results}"
)


@dataclass
class PulseState:
    goal: str
    todos: list[Todo] = field(default_factory=list)
    step_outputs: list[str] = field(default_factory=list)


@dataclass
class PulseDeps:
    agent: Agent
    agent_deps: object
    message_history: list[ModelMessage] | None = None


def parse_steps(raw: str) -> list[str]:
    """Parse a numbered-list response into step instructions.

    Only lines starting with a digit followed by ``.`` or ``)`` are treated
    as steps.  Free-form prose is silently ignored.
    """
    steps: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        match = re.match(r"^\d+[.)]\s*(.+)", line)
        if match:
            body = match.group(1).strip()
            if body:
                steps.append(body)
    return steps


def extract_text(result: object) -> str:
    """Pull a plain-text string out of an agent run result."""
    output = getattr(result, "output", result)
    if isinstance(output, str):
        return output
    return str(output)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


@dataclass
class Classify(BaseNode[PulseState, PulseDeps, str]):
    """Decide whether the request needs multi-step planning or a direct answer."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> Decompose | End[str]:
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=CLASSIFY_INSTRUCTION,
        )
        answer = extract_text(result).strip().lower()
        if answer.startswith("direct"):
            direct = await ctx.deps.agent.run(
                ctx.state.goal,
                deps=ctx.deps.agent_deps,
                message_history=ctx.deps.message_history,
            )
            return End(extract_text(direct))
        return Decompose()


@dataclass
class Decompose(BaseNode[PulseState, PulseDeps, str]):
    """Break the request into numbered steps backed by Todo items."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | End[str]:
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=DECOMPOSE_INSTRUCTION,
        )
        instructions = parse_steps(extract_text(result))
        if not instructions:
            direct = await ctx.deps.agent.run(
                ctx.state.goal,
                deps=ctx.deps.agent_deps,
                message_history=ctx.deps.message_history,
            )
            return End(extract_text(direct))

        for i, instruction in enumerate(instructions):
            ctx.state.todos.append(
                Todo(todo_id=f"pulse-{i}", title=instruction, status="pending")
            )
        return ExecuteStep()


@dataclass
class ExecuteStep(BaseNode[PulseState, PulseDeps, str]):
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
        instructions = STEP_INSTRUCTION.format(
            goal=ctx.state.goal,
            context=context,
            instruction=current.title,
        )
        result = await ctx.deps.agent.run(
            current.title,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=instructions,
        )
        output = extract_text(result)
        ctx.state.step_outputs.append(f"- {current.title}: {output}")

        ctx.state.todos = [
            t.model_copy(update={"status": "done"})
            if t.todo_id == current.todo_id
            else t
            for t in ctx.state.todos
        ]
        return ExecuteStep()


@dataclass
class Synthesize(BaseNode[PulseState, PulseDeps, str]):
    """Produce the final user-facing answer from all step outputs."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> End[str]:
        results_block = "\n".join(ctx.state.step_outputs)
        instructions = SYNTHESIZE_INSTRUCTION.format(
            goal=ctx.state.goal, results=results_block
        )
        result = await ctx.deps.agent.run(
            "Synthesize the final answer.",
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            instructions=instructions,
        )
        return End(extract_text(result))


pulse_graph = Graph(nodes=[Classify, Decompose, ExecuteStep, Synthesize])


async def run_pulse(
    agent: Agent,
    deps: object,
    prompt: str | list,
    *,
    message_history: list[ModelMessage] | None = None,
) -> str:
    """Run the pulse pipeline: classify → optionally decompose → execute → synthesize.

    Simple questions are answered directly.  Complex multi-step requests are
    decomposed into Todo-backed steps and executed sequentially.
    Returns only the final user-facing answer.
    """
    goal = prompt if isinstance(prompt, str) else " ".join(str(p) for p in prompt)
    state = PulseState(goal=goal)
    pulse_deps = PulseDeps(
        agent=agent, agent_deps=deps, message_history=message_history
    )
    result = await pulse_graph.run(Classify(), state=state, deps=pulse_deps)
    return result.output
