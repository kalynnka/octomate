"""Pulse — lightweight internal planning for medium-complexity tasks.

Uses pydantic-ai's graph to triage requests, decompose eligible ones into
a sequence of steps backed by Todo items, execute each step via Agent.run(),
and return only the final synthesis.  Simple questions are answered directly
without planning.

Usage::

    answer = await run_pulse(agent, deps, "Summarize, compare, recommend")
    # *answer* is a clean, user-facing string — no plan artifacts exposed.
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


TRIAGE_INSTRUCTION = (
    "If the request below is a simple question, answer it directly.\n"
    "If it requires multiple steps, break it into a numbered list of "
    "concrete steps (2-5). Output ONLY the numbered list — no commentary.\n"
    "Example of a numbered list:\n"
    "1. Summarize the key points of X\n"
    "2. Compare them against Y\n"
    "3. Synthesize a final recommendation"
)

STEP_INSTRUCTION = (
    "Complete ONLY the step described below and return ONLY the result. "
    "Do not mention the plan, other steps, or any meta-commentary.\n\n"
    "Overall goal: {goal}\n"
    "Previous context:\n{context}\n\n"
    "Current step: {instruction}"
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


@dataclass
class Triage(BaseNode[PulseState, PulseDeps, str]):
    """Classify the request and either answer directly or decompose into steps.

    The agent is asked to either answer a simple question directly or produce
    a numbered step list.  Parsing determines the path: numbered steps lead
    to ExecuteStep; plain prose is returned as the direct answer.  This keeps
    simple questions to a single LLM call.
    """

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> ExecuteStep | End[str]:
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            output_type=str,
            instructions=TRIAGE_INSTRUCTION,
        )
        instructions = parse_steps(result.output)
        if not instructions:
            return End(result.output)

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
        step_instructions = STEP_INSTRUCTION.format(
            goal=ctx.state.goal,
            context=context,
            instruction=current.title,
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
class Synthesize(BaseNode[PulseState, PulseDeps, str]):
    """Produce the final user-facing answer from all step outputs."""

    async def run(
        self, ctx: GraphRunContext[PulseState, PulseDeps]
    ) -> End[str]:
        results_block = "\n".join(ctx.state.step_outputs)
        synth_instructions = SYNTHESIZE_INSTRUCTION.format(results=results_block)
        result = await ctx.deps.agent.run(
            ctx.state.goal,
            deps=ctx.deps.agent_deps,
            message_history=ctx.deps.message_history,
            output_type=str,
            instructions=synth_instructions,
        )
        return End(result.output)


pulse_graph = Graph(nodes=[Triage, ExecuteStep, Synthesize])


async def run_pulse(
    agent: Agent,
    deps: object,
    prompt: str | list,
    *,
    message_history: list[ModelMessage] | None = None,
) -> str:
    """Run the pulse pipeline: triage → optionally execute steps → synthesize.

    Simple questions are answered directly in a single call.  Complex multi-step
    requests are decomposed into Todo-backed steps and executed sequentially.
    Returns only the final user-facing answer.
    """
    goal = prompt if isinstance(prompt, str) else " ".join(str(p) for p in prompt)
    state = PulseState(goal=goal)
    pulse_deps = PulseDeps(
        agent=agent, agent_deps=deps, message_history=message_history
    )
    result = await pulse_graph.run(Triage(), state=state, deps=pulse_deps)
    return result.output
