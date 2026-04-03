"""Pulse — lightweight internal planning for medium-complexity tasks.

Decomposes eligible requests into a sequence of steps, executes each step
using pydantic-ai's ``Agent.run()`` directly, and returns **only** the final
synthesis result.  No intermediate reasoning, plan structure, or step details
are ever returned in the chat.

Usage::

    plan = await decompose(agent, deps, user_prompt)
    result = await execute_plan(agent, deps, plan)
    # *result* is the clean, user-facing answer string.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic_ai import Agent

from octomate.schemas.plan import Plan, PlanStep, StepStatus

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


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


def _parse_steps(raw: str, goal: str) -> Plan:
    """Parse a numbered-list response into a Plan.

    Only lines that start with a digit followed by a separator (`.` or `)`) are
    treated as steps.  Anything else is silently ignored so that the caller can
    fall back to a single-step plan when the model returns free-form prose.
    """
    steps: list[PlanStep] = []
    for line in raw.splitlines():
        line = line.strip()
        match = re.match(r"^\d+[.)]\s*(.+)", line)
        if match:
            body = match.group(1).strip()
            if body:
                steps.append(PlanStep(index=len(steps), instruction=body))
    return Plan(goal=goal, steps=steps)


async def decompose(
    agent: Agent,
    deps: object,
    user_prompt: str | list,
    *,
    message_history: list[ModelMessage] | None = None,
) -> Plan:
    """Ask the agent to decompose *user_prompt* into a lightweight Plan.

    Returns a ``Plan`` with ≥1 step.  If the model returns something
    unparseable, a single-step plan echoing the original prompt is created
    as a fallback so execution can always proceed.
    """
    result = await agent.run(
        user_prompt,
        deps=deps,
        message_history=message_history,
        instructions=DECOMPOSE_INSTRUCTION,
    )
    raw = _extract_text(result)
    plan = _parse_steps(raw, goal=_flatten_prompt(user_prompt))
    if not plan.steps:
        plan.steps = [
            PlanStep(index=0, instruction=_flatten_prompt(user_prompt)),
        ]
    return plan


async def execute_plan(
    agent: Agent,
    deps: object,
    plan: Plan,
    *,
    message_history: list[ModelMessage] | None = None,
) -> str:
    """Execute every step of *plan* in order, then synthesize a final answer.

    Returns the clean, user-facing answer string.  All intermediate artefacts
    remain internal.
    """
    context_parts: list[str] = []

    for step in plan.steps:
        step.status = StepStatus.RUNNING
        context = "\n".join(context_parts) if context_parts else "(none)"
        instructions = STEP_INSTRUCTION.format(
            goal=plan.goal,
            context=context,
            instruction=step.instruction,
        )
        try:
            result = await agent.run(
                step.instruction,
                deps=deps,
                message_history=message_history,
                instructions=instructions,
            )
            step.output = _extract_text(result)
            step.status = StepStatus.DONE
        except Exception:
            step.status = StepStatus.FAILED
            step.output = ""
            raise

        context_parts.append(f"- {step.instruction}: {step.output}")

    results_block = "\n".join(
        f"Step {s.index + 1}: {s.output}" for s in plan.steps if s.status == StepStatus.DONE
    )
    synth_instructions = SYNTHESIZE_INSTRUCTION.format(
        goal=plan.goal, results=results_block
    )
    final = await agent.run(
        "Synthesize the final answer.",
        deps=deps,
        message_history=message_history,
        instructions=synth_instructions,
    )
    return _extract_text(final)


def _extract_text(result: object) -> str:
    """Pull a plain-text string out of an agent run result."""
    output = getattr(result, "output", result)
    if isinstance(output, str):
        return output
    return str(output)


def _flatten_prompt(prompt: str | list) -> str:
    if isinstance(prompt, str):
        return prompt
    return " ".join(str(p) for p in prompt)
