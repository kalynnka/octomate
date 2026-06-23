from __future__ import annotations

from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from octomate.capabilities.summon import (
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    SummonCapability,
)
from octomate.schemas.triage import SummonDecision, SummonRoute

FAKE_CONTEXT = cast(RunContext[None], None)


def _capability() -> SummonCapability:
    return SummonCapability(
        routes=[
            SummonRoute(
                agent_id="claude",
                model="opus",
                description="coding work",
            ),
            SummonRoute(
                agent_id="inkling",
                model="flash",
                description="current agent",
            ),
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
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


def test_summon_instructions_keep_target_separate_from_route() -> None:
    instructions = _capability().get_instructions()

    assert "You have two tools" in instructions
    assert "`scry`" in instructions
    assert "If you can answer directly" in instructions
    assert "agent_id=claude" not in instructions
    assert "inkling" not in instructions
    assert "coding work" not in instructions
    assert "target_id" not in instructions


async def test_scry_tool_returns_summonable_routes() -> None:
    capability = _capability()
    assert capability.toolset is not None
    scry = capability.toolset.tools[SCRY_TOOL_NAME].function

    routes = await scry(FAKE_CONTEXT)

    assert routes == [
        SummonRoute(
            agent_id="claude",
            model="opus",
            description="coding work",
        )
    ]


async def test_summon_capability_rejects_self_summon() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="Cannot summon yourself"):
        await summon(
            FAKE_CONTEXT,
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
            FAKE_CONTEXT,
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
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert result == "Summoning claude (opus)."
    assert capability.decision == _decision()
