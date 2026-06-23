from __future__ import annotations

import pytest
from pydantic_ai.exceptions import ModelRetry

from octomate.capabilities.summon import SUMMON_TOOL_NAME, SummonCapability
from octomate.schemas.triage import SummonDecision, SummonRoute


def _capability() -> SummonCapability:
    return SummonCapability(
        routes=[
            SummonRoute(
                agent_id="claude",
                model="opus",
                description="coding work",
            )
        ],
        current_agent_id="inkling",
    )


def _decision(agent_id: str = "claude", model: str = "opus") -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model=model,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )


async def test_summon_capability_accepts_exact_route() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        None,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


def test_summon_instructions_keep_target_separate_from_route() -> None:
    instructions = _capability().get_instructions()

    assert "You have a `summon` tool" in instructions
    assert "If you can answer directly" in instructions
    assert "agent_id=claude, model=opus" in instructions
    assert "target_id" not in instructions


async def test_summon_capability_rejects_self_summon() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Cannot summon current agent"):
        await summon(
            None,
            agent_id="inkling",
            model="opus",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_tool_retries_invalid_route() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Invalid summon route"):
        await summon(
            None,
            agent_id="claude",
            model="sonnet",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_tool_records_decision() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    result = await summon(
        None,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert result == "Summoning claude (opus)."
    assert capability.decision == _decision()
