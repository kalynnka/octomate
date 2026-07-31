"""The `tool_output` block of the inkling config, and the band semantics it names.

The translation from this config into `ToolOutputLimits` is inline in `main.py`, so
what is covered here is either side of it: that the config parses and defaults the
way it claims, and that the harness behaves the way the config's wording promises.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.tool_output_limits import (
    READ_TOOL_NAME,
    Band,
    Spill,
    Summarize,
    ToolOutputLimits,
    Truncate,
)

from octomate.config.agents import (
    InklingConfig,
    SpillAction,
    SummarizeAction,
    ToolOutputConfig,
    TruncateAction,
)


class BrokenStore:
    """A store that refuses every write — a DB outage, or an unapplied migration."""

    async def write(self, key: str, data: bytes) -> str:
        raise OSError("no such table: tool_output_spills")

    async def read(self, handle: str) -> bytes:
        raise FileNotFoundError(handle)


async def fetch_through(limits: ToolOutputLimits[None], size: int) -> str:
    """Run a tool returning `size` characters, and report what reached history."""

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        called = any(
            isinstance(part, ToolCallPart) and part.tool_name == "fetch"
            for message in messages
            for part in getattr(message, "parts", [])
        )
        if not called:
            return ModelResponse(parts=[ToolCallPart("fetch", {})])
        # Also stands in for the summarizer, which runs on the agent's own model.
        return ModelResponse(parts=[TextPart("a summary")])

    agent = Agent(
        FunctionModel(reply),
        deps_type=type(None),
        output_type=str,
        capabilities=[limits],
    )

    @agent.tool_plain
    def fetch() -> str:
        return "y" * size

    result = await agent.run("go")
    return next(
        str(part.content)
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart) and part.tool_name == "fetch"
    )


def test_default_spills_the_large_and_summarizes_the_enormous() -> None:
    """Left unconfigured: nothing under 10k is touched, the merely-large is kept
    whole behind a handle, and only the truly enormous pays for a summary."""

    bands = ToolOutputConfig().bands

    assert [band.over for band in bands] == [10_000, 200_000]
    assert isinstance(bands[0].action, SpillAction)
    assert isinstance(bands[1].action, SummarizeAction)


def test_retention_is_offered_as_a_timedelta() -> None:
    assert ToolOutputConfig(retention_hours=2).retention == timedelta(hours=2)
    assert ToolOutputConfig(retention_hours=None).retention is None


def test_action_is_a_discriminated_union() -> None:
    """`kind` picks the variant, so an unknown action is rejected at config load
    rather than silently mounting something else."""

    parsed = InklingConfig.model_validate(
        {
            "models": [{"name": "deepseek:deepseek-v4-pro"}],
            "tool_output": {
                "bands": [
                    {"over": 4_000, "action": {"kind": "truncate", "max_chars": 9}}
                ]
            },
        }
    )
    action = parsed.tool_output.bands[0].action
    assert isinstance(action, TruncateAction) and action.max_chars == 9

    with pytest.raises(ValidationError):
        ToolOutputConfig.model_validate(
            {"bands": [{"over": 4_000, "action": {"kind": "compress"}}]}
        )


def test_empty_bands_is_rejected() -> None:
    """An empty list reduces nothing, which `enabled: false` already says clearly."""

    with pytest.raises(ValidationError):
        ToolOutputConfig(bands=[])


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (3_000, "untouched"),
        (8_000, "truncated"),
        (50_000, "summarized"),
        (150_000, "spilled"),
    ],
)
async def test_the_largest_band_a_return_reaches_claims_it(
    size: int, expected: str
) -> None:
    """What the config means by "largest band wins" — bands declared smallest-first
    still route by size, so the order written in yaml carries no meaning."""

    limits = ToolOutputLimits(
        bands=[
            Band(over=5_000, action=Truncate(max_chars=500)),
            Band(over=20_000, action=Summarize()),
            Band(over=100_000, action=Spill()),
        ]
    )

    returned = await fetch_through(limits, size)

    outcome = (
        "spilled"
        if READ_TOOL_NAME in returned
        else "summarized"
        if returned == "a summary"
        else "truncated"
        if len(returned) < size
        else "untouched"
    )
    assert outcome == expected


async def test_a_spill_that_cannot_be_stored_truncates() -> None:
    """What the config means by "falls back to truncation": with nowhere to spill
    to, the model gets a bounded slice rather than the 50k it asked about."""

    limits = ToolOutputLimits(
        bands=[Band(over=10_000, action=Spill(then=Truncate()))],
        store=BrokenStore(),
    )

    returned = await fetch_through(limits, 50_000)
    assert READ_TOOL_NAME not in returned
    assert len(returned) < 50_000
