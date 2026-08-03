"""`ToolOutputLimits` as inkling mounts it: a tentacle capability like any other.

It rides every run the tentacle makes, accomplices included — they call the same
tools, so the same bound applies.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.tool_output_limits import (
    READ_TOOL_NAME,
    Band,
    Spill,
    ToolOutputLimits,
    Truncate,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import ToolOutputConfig
from octomate.managers.spills import SpillStore
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.agents.inkling import InklingTentacle
from tests.support.managers import FakeConversationManager

# Well past the 10k-char band the capability defaults to.
HUGE_TOOL_OUTPUT = "x" * 50_000

_THREAD = uuid7()

STR_OUTPUT = [str]


def _address() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="test",
        chat_type="private",
        chat_id="test",
        user_id="test",
    )


def dump_the_lot() -> str:
    """Stands in for an MCP server answering with far more than anyone asked for."""
    return HUGE_TOOL_OUTPUT


async def _call_then_answer(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    already_called = any(
        isinstance(part, ToolCallPart) and part.tool_name == "dump_the_lot"
        for message in messages
        for part in getattr(message, "parts", [])
    )
    if not already_called:
        yield {0: DeltaToolCall(name="dump_the_lot", json_args="{}")}
        return
    yield "read it"


def _tentacle() -> tuple[InklingTentacle, FakeConversationManager]:
    conversations = FakeConversationManager()
    # Mirrors what main.py assembles from the default `tool_output` config.
    limits = ToolOutputLimits(
        bands=[Band(over=10_000, action=Spill(then=Truncate()))],
        store=SpillStore(retention=ToolOutputConfig().retention),
    )
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        models={
            "scripted": FunctionModel(
                stream_function=_call_then_answer, model_name="scripted"
            )
        },
        toolsets=[FunctionToolset([dump_the_lot], id="dump")],
        capabilities=[limits],
        conversation_manager=conversations,
    )
    return tentacle, conversations


def _tool_returns(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart) and part.tool_name == "dump_the_lot"
    ]


async def test_oversized_tool_return_is_reduced_before_it_reaches_history(
    in_memory_engine: AsyncEngine,
) -> None:
    """A tool return persists and is re-sent on every later request, so the
    reduction has to land in history — not just in what this turn's model saw."""

    tentacle, _ = _tentacle()

    result = await tentacle.run(
        "dump it",
        conversation_address=_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )

    returns = _tool_returns(result.all_messages())
    assert returns, "the scripted model should have called dump_the_lot"
    assert all(len(content) < len(HUGE_TOOL_OUTPUT) for content in returns)
    # Lossless by default: the model is handed a handle rather than losing the tail,
    # and that handle resolves — through the database, so any process can honour it.
    assert any(READ_TOOL_NAME in content for content in returns)
    handle = re.search(r"stored to handle '([^']+)'", returns[0])
    assert handle is not None, returns[0]
    spilled = await SpillStore().read(handle.group(1))
    assert spilled.decode() == HUGE_TOOL_OUTPUT


async def test_accomplice_is_bounded_without_being_asked(
    in_memory_engine: AsyncEngine,
) -> None:
    """An accomplice calls the same unbounded MCP tools as the run that spawned
    it, so it is served the same tentacle capabilities — its spawner does not
    have to know to ask for them."""

    tentacle, conversations = _tentacle()
    parent = await conversations.ensure(_THREAD, agent_tentacle_id="inkling")
    child = await conversations.ensure(
        _THREAD,
        agent_tentacle_id="inkling",
        subagent_id="accomplice",
        parent_conversation_id=parent.id,
    )

    result = await tentacle.subagent_run(
        "dump it",
        conversation_address=_address(),
        thread_id=_THREAD,
        conversation_id=child.id,
    )

    returns = _tool_returns(result.all_messages())
    assert returns, "the accomplice should have called dump_the_lot"
    assert all(len(content) < len(HUGE_TOOL_OUTPUT) for content in returns)
    assert any(READ_TOOL_NAME in content for content in returns)
