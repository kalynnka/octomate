from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.exceptions import ModelRetry

from octomate.capabilities.gate import (
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    GateCapability,
)
from octomate.schemas.triage import SummonDecision, SummonDestination, SummonRoute

FAKE_CONTEXT = cast(RunContext[None], None)


def _capability(allow_here: bool = True) -> GateCapability:
    return GateCapability(
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
        allow_here=allow_here,
    )


def _decision(
    agent_id: str = "claude",
    model: str = "opus",
    destination: SummonDestination = "thread",
) -> SummonDecision:
    return SummonDecision(
        action="summon",
        agent_id=agent_id,
        model=model,
        destination=destination,
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )


def test_summon_decision_requires_model_field() -> None:
    with pytest.raises(ValidationError, match="model"):
        SummonDecision(
            action="summon",
            agent_id="claude",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


@pytest.mark.parametrize("model", [None, ""])
def test_summon_decision_requires_concrete_model(model: str | None) -> None:
    with pytest.raises(ValidationError, match="model"):
        SummonDecision(
            action="summon",
            agent_id="claude",
            model=model,
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


def test_summon_decision_defaults_to_thread_destination() -> None:
    assert _decision().destination == "thread"


async def test_summon_capability_accepts_exact_route() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="thread",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision()


def test_gate_instruction_explains_each_spell_in_plain_words() -> None:
    instructions = _capability().get_instructions()

    assert "`scry`" in instructions
    assert "`summon`" in instructions
    assert "`teleport`" in instructions
    assert "route the conversation" in instructions
    # No concrete route details leak into the shared instruction block.
    assert "agent_id=claude" not in instructions
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
            destination="thread",
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
            destination="thread",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )


async def test_summon_here_refused_when_disallowed() -> None:
    capability = _capability(allow_here=False)
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    with pytest.raises(ModelRetry, match="group's main channel"):
        await summon(
            FAKE_CONTEXT,
            agent_id="claude",
            model="opus",
            destination="here",
            reason="needs coding",
            hint="Working on it",
            summon="Please investigate the failing test.",
        )
    assert capability.decision is None


async def test_summon_here_allowed_on_bounded_surface() -> None:
    capability = _capability(allow_here=True)
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="here",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert capability.decision == _decision(destination="here")


async def test_summon_tool_records_decision() -> None:
    capability = _capability()
    assert capability.toolset is not None
    summon = capability.toolset.tools[SUMMON_TOOL_NAME].function

    result = await summon(
        FAKE_CONTEXT,
        agent_id="claude",
        model="opus",
        destination="thread",
        reason="needs coding",
        hint="Working on it",
        summon="Please investigate the failing test.",
    )

    assert result == "Summoning claude (opus) → thread."
    assert capability.decision == _decision()


async def test_teleport_defers_the_run() -> None:
    capability = _capability()
    assert capability.toolset is not None
    teleport = capability.toolset.tools[TELEPORT_TOOL_NAME].function

    with pytest.raises(CallDeferred):
        await teleport(FAKE_CONTEXT, hint="let's move to a thread")
