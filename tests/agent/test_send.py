"""SendCapability (emit-only): the send_message tool returns "sent" and announces
a MessageSentEvent on the run's event stream. The tool touches no channel and
resolves no conversation — the consumer rendering the run delivers the segments.
The capability is wired into the inkling agent, so a scripted send_message call
surfaces a MessageSentEvent end-to-end.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any, cast

from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from octomate.capabilities.events import MessageSentEvent
from octomate.capabilities.send import SendCapability
from octomate.config import ModelConfig
from octomate.providers import ProviderRegistry
from octomate.schemas.segments import MarkdownSegment, OutputSegment
from octomate.tentacles.agent.inkling import build_inkling_agent

from pydantic_ai.models.test import TestModel

from tests.support.agents import ScriptedStream, ScriptedTurn


class StubRegistry:
    """A credential-free TestModel so build_inkling_agent runs without a provider."""

    def build_model(
        self, model: ModelConfig, settings: object | None = None
    ) -> TestModel:
        return TestModel()


async def test_send_message_returns_sent_and_announces_event() -> None:
    capability = SendCapability()
    assert capability.toolset is not None
    send_message = capability.toolset.tools["send_message"].function
    segments: list[OutputSegment] = [MarkdownSegment(data={"text": "halfway there"})]

    result = await send_message(cast(RunContext[Any], None), segments)

    assert isinstance(result, ToolReturn)
    assert result.return_value == "sent"
    assert result.metadata == [MessageSentEvent(segments=segments)]


def test_send_message_tool_exposes_no_channel_fields() -> None:
    capability = SendCapability()
    assert capability.toolset is not None
    send_message = capability.toolset.tools["send_message"].function
    params = set(inspect.signature(send_message).parameters)
    assert params == {"ctx", "segments"}


async def test_wrap_forwards_stashed_message_sent_event() -> None:
    event = MessageSentEvent(segments=[MarkdownSegment(data={"text": "sent it"})])
    result_event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="send_message",
            content="sent",
            tool_call_id="c1",
            metadata=[event],
        )
    )

    async def fake_stream() -> AsyncIterator[AgentStreamEvent]:
        yield result_event

    out = [
        forwarded
        async for forwarded in SendCapability().wrap_run_event_stream(
            cast(RunContext[Any], None), stream=fake_stream()
        )
    ]

    assert out[0] is result_event
    assert out[1:] == [event]


async def test_inkling_agent_send_tool_surfaces_event_end_to_end() -> None:
    # build_inkling_agent wires SendCapability: a scripted send_message call
    # surfaces a MessageSentEvent on the run's stream.
    agent = build_inkling_agent(
        cast(ProviderRegistry, StubRegistry()),
        ModelConfig(provider="vertex", name="gemini-3-flash-preview"),
    )
    conversation_id = "11111111-1111-1111-1111-111111111111"
    script = ScriptedStream(
        turns=[
            ScriptedTurn(
                tool_name="send_message",
                args={"segments": [{"type": "markdown", "data": {"text": "progress"}}]},
                tool_call_id="call_send_1",
            ),
            "done",
        ]
    )
    events = [
        event
        async for event in agent.stream_events(
            "send something",
            conversation_id=conversation_id,
            model=FunctionModel(stream_function=script, model_name="scripted"),
        )
    ]

    sent = [event for event in events if isinstance(event, MessageSentEvent)]
    assert len(sent) == 1
    assert [str(segment) for segment in sent[0].segments] == ["progress"]
