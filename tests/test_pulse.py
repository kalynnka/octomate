"""Tests for the Pulse planning and execution pipeline."""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.pulse import (
    DECOMPOSE_INSTRUCTION,
    STEP_INSTRUCTION,
    SYNTHESIZE_INSTRUCTION,
    _flatten_prompt,
    _parse_steps,
    decompose,
    execute_plan,
)
from octomate.schemas.plan import Plan, PlanStep, StepStatus


# ---------------------------------------------------------------------------
# Plan / PlanStep schema tests
# ---------------------------------------------------------------------------


class TestPlanStep:
    def test_defaults(self):
        step = PlanStep(index=0, instruction="do stuff")
        assert step.status == StepStatus.PENDING
        assert step.output == ""

    def test_round_trip_json(self):
        step = PlanStep(index=1, instruction="compare", output="result", status=StepStatus.DONE)
        restored = PlanStep.model_validate_json(step.model_dump_json())
        assert restored == step


class TestPlan:
    def test_current_step_returns_first_pending(self):
        plan = Plan(
            goal="test",
            steps=[
                PlanStep(index=0, instruction="a", status=StepStatus.DONE),
                PlanStep(index=1, instruction="b", status=StepStatus.PENDING),
                PlanStep(index=2, instruction="c", status=StepStatus.PENDING),
            ],
        )
        assert plan.current_step is not None
        assert plan.current_step.index == 1

    def test_current_step_none_when_complete(self):
        plan = Plan(
            goal="test",
            steps=[PlanStep(index=0, instruction="a", status=StepStatus.DONE)],
        )
        assert plan.current_step is None

    def test_is_complete(self):
        plan = Plan(
            goal="test",
            steps=[
                PlanStep(index=0, instruction="a", status=StepStatus.DONE),
                PlanStep(index=1, instruction="b", status=StepStatus.DONE),
            ],
        )
        assert plan.is_complete

    def test_not_complete_while_pending(self):
        plan = Plan(
            goal="test",
            steps=[
                PlanStep(index=0, instruction="a", status=StepStatus.DONE),
                PlanStep(index=1, instruction="b", status=StepStatus.PENDING),
            ],
        )
        assert not plan.is_complete

    def test_failed_property(self):
        plan = Plan(
            goal="test",
            steps=[
                PlanStep(index=0, instruction="a", status=StepStatus.DONE),
                PlanStep(index=1, instruction="b", status=StepStatus.FAILED),
            ],
        )
        assert plan.failed

    def test_empty_plan_is_complete(self):
        plan = Plan(goal="test")
        assert plan.is_complete
        assert plan.current_step is None


# ---------------------------------------------------------------------------
# _parse_steps unit tests
# ---------------------------------------------------------------------------


class TestParseSteps:
    def test_numbered_list(self):
        raw = "1. Summarize X\n2. Compare Y\n3. Synthesize"
        plan = _parse_steps(raw, "goal")
        assert len(plan.steps) == 3
        assert plan.steps[0].instruction == "Summarize X"
        assert plan.steps[2].instruction == "Synthesize"

    def test_blank_lines_skipped(self):
        raw = "1. A\n\n2. B\n"
        plan = _parse_steps(raw, "g")
        assert len(plan.steps) == 2

    def test_paren_numbering(self):
        raw = "1) Alpha\n2) Beta"
        plan = _parse_steps(raw, "g")
        assert plan.steps[0].instruction == "Alpha"

    def test_returns_empty_for_gibberish(self):
        plan = _parse_steps("", "g")
        assert plan.steps == []

    def test_goal_is_set(self):
        plan = _parse_steps("1. A", "my goal")
        assert plan.goal == "my goal"


# ---------------------------------------------------------------------------
# _flatten_prompt tests
# ---------------------------------------------------------------------------


class TestFlattenPrompt:
    def test_string_passthrough(self):
        assert _flatten_prompt("hello world") == "hello world"

    def test_list_joined(self):
        assert _flatten_prompt(["hello", "world"]) == "hello world"


# ---------------------------------------------------------------------------
# decompose() integration test (FunctionModel)
# ---------------------------------------------------------------------------


def _plan_model(steps_text: str) -> FunctionModel:
    """FunctionModel that returns a fixed numbered list as plain text."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        _ = messages, info
        return ModelResponse(parts=[TextPart(content=steps_text)])

    return FunctionModel(fn)


async def test_decompose_produces_plan():
    agent: Agent[None, str] = Agent("test", output_type=str)
    plan_text = "1. Summarize the article\n2. Compare with previous findings\n3. Synthesize a recommendation"
    with agent.override(model=_plan_model(plan_text)):
        plan = await decompose(agent, deps=None, user_prompt="Analyze the report")

    assert plan.goal == "Analyze the report"
    assert len(plan.steps) == 3
    assert all(s.status == StepStatus.PENDING for s in plan.steps)


async def test_decompose_fallback_on_unparseable_response():
    """When the model returns text that doesn't parse into numbered steps,
    decompose falls back to a single-step plan echoing the original prompt."""
    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=_plan_model("I'm not sure what to do here.")):
        plan = await decompose(agent, deps=None, user_prompt="just do it")

    assert len(plan.steps) == 1
    assert plan.steps[0].instruction == "just do it"


# ---------------------------------------------------------------------------
# execute_plan() integration test (FunctionModel)
# ---------------------------------------------------------------------------


def _echo_model() -> FunctionModel:
    """FunctionModel that echoes the user prompt back as plain text."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        _ = info
        # Extract last user message text
        for m in reversed(messages):
            for part in getattr(m, "parts", []):
                if hasattr(part, "content") and isinstance(part.content, str):
                    return ModelResponse(parts=[TextPart(content=f"[done] {part.content}")])
        return ModelResponse(parts=[TextPart(content="[done]")])

    return FunctionModel(fn)


async def test_execute_plan_runs_all_steps_and_synthesizes():
    agent: Agent[None, str] = Agent("test", output_type=str)
    plan = Plan(
        goal="Summarize, compare, synthesize",
        steps=[
            PlanStep(index=0, instruction="Summarize X"),
            PlanStep(index=1, instruction="Compare Y"),
            PlanStep(index=2, instruction="Synthesize"),
        ],
    )
    with agent.override(model=_echo_model()):
        answer = await execute_plan(agent, deps=None, plan=plan)

    # All steps should be marked done
    assert all(s.status == StepStatus.DONE for s in plan.steps)
    assert plan.is_complete
    # The answer is a string (the synthesis output)
    assert isinstance(answer, str)
    assert len(answer) > 0


async def test_execute_plan_step_outputs_populated():
    agent: Agent[None, str] = Agent("test", output_type=str)
    plan = Plan(
        goal="test",
        steps=[
            PlanStep(index=0, instruction="step-one"),
            PlanStep(index=1, instruction="step-two"),
        ],
    )
    with agent.override(model=_echo_model()):
        await execute_plan(agent, deps=None, plan=plan)

    for step in plan.steps:
        assert step.output != ""
        assert step.status == StepStatus.DONE


async def test_full_pipeline_prompt_to_clean_answer():
    """End-to-end: user prompt → decompose → execute → clean string answer."""
    call_count = 0

    def counting_model_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # decompose call → return plan
            return ModelResponse(
                parts=[TextPart(content="1. Summarize\n2. Compare\n3. Synthesize")]
            )
        # step + synthesis calls → echo
        for m in reversed(messages):
            for part in getattr(m, "parts", []):
                if hasattr(part, "content") and isinstance(part.content, str):
                    return ModelResponse(
                        parts=[TextPart(content=f"Result of: {part.content}")]
                    )
        return ModelResponse(parts=[TextPart(content="ok")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(counting_model_fn)):
        plan = await decompose(agent, deps=None, user_prompt="Analyze the report")
        answer = await execute_plan(agent, deps=None, plan=plan)

    # 1 decompose + 3 steps + 1 synthesis = 5 calls
    assert call_count == 5
    assert isinstance(answer, str)
    assert len(answer) > 0
    # No internal plan artifacts leaked into the answer
    assert "step" not in answer.lower() or "Result of" in answer
    assert plan.is_complete
    assert not plan.failed
