"""The gate's `send` tool: returns "sent" and announces a MessageSentEvent
on the run's event stream, which the consumer rendering the run delivers. The tool
names `here` or `dm` and resolves neither — but the gate it lives on knows the
channel's surfaces, so an unreachable `dm` is refused rather than redirected.
"""

from __future__ import annotations

import inspect

import pytest
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.capabilities.gateway import GatewayCapability
from octomate.tentacles.channel.base import ChannelSurfaces
from octomate.capabilities.todos import TodoCapability
from octomate.schemas.segments import MarkdownSegment, MessageSegment
from octomate.capabilities.ask import AskCapability
from octomate.schemas.conversation import ChannelAddress
from tests.support.channels import FakeChannelTentacle
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from tests.support.agents import ScriptedStream, ScriptedTurn


class _NoDmChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)


def _gate(
    *, direct_messages: bool = True, already_private: bool = False
) -> GatewayCapability:
    """A routing-only gate: on a channel with direct messages unless asked
    otherwise, answering from a group unless asked to be in the DM already."""
    return GatewayCapability(
        routes=[],
        current_agent_id="inkling",
        channels={"im": FakeChannelTentacle() if direct_messages else _NoDmChannel()},
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="private" if already_private else "group",
            chat_id="room",
            user_id="alice",
        ),
    )


def _inkling_agent() -> Agent[None, InklingOutput]:
    return Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        capabilities=[AskCapability(), TodoCapability(), _gate()],
        system_prompt=SYSTEM_PROMPT,
    )


async def test_send_returns_sent_and_announces_event() -> None:
    capability = _gate()
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "halfway there"})]

    result = await send(cast(RunContext[Any], None), segments)

    assert isinstance(result, ToolReturn)
    assert result.return_value == "sent"
    assert result.metadata == [MessageSentEvent(segments=segments)]


def test_send_tool_exposes_no_channel_fields() -> None:
    # `destination` names one of a closed set; no channel, chat or user id ever
    # reaches the tool, which is what keeps the schema constant across runs.
    capability = _gate()
    assert capability.toolset is not None
    tool = capability.toolset.tools["send"]
    params = set(inspect.signature(tool.function).parameters)
    assert params == {"ctx", "segments", "destination"}

    schema = tool.tool_def.parameters_json_schema
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["destination"]["enum"] == ["here", "dm"]


async def test_send_carries_the_named_destination() -> None:
    capability = _gate()
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, "dm")

    assert result.metadata == [MessageSentEvent(segments=segments, destination="dm")]


async def test_wrap_forwards_stashed_message_sent_event() -> None:
    event = MessageSentEvent(segments=[MarkdownSegment(data={"text": "sent it"})])
    result_event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="send",
            content="sent",
            tool_call_id="c1",
            metadata=[event],
        )
    )

    async def fake_stream() -> AsyncIterator[AgentStreamEvent]:
        yield result_event

    out = [
        forwarded
        async for forwarded in _gate().wrap_run_event_stream(
            cast(RunContext[Any], None), stream=fake_stream()
        )
    ]

    assert out[0] is result_event
    assert out[1:] == [event]


async def test_inkling_agent_send_tool_surfaces_event_end_to_end() -> None:
    # The inkling agent assembly mounts the gate: a scripted `send`
    # call surfaces a MessageSentEvent on the run's stream.
    agent = _inkling_agent()
    conversation_id = "11111111-1111-1111-1111-111111111111"
    script = ScriptedStream(
        turns=[
            ScriptedTurn(
                tool_name="send",
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


async def test_send_to_dm_is_refused_where_there_are_none() -> None:
    # The reason the merge was worth it: on its own the tool knew no surfaces and
    # the consumer had to redirect silently. On the gate it can say why up front.
    capability = _gate(direct_messages=False)
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    with pytest.raises(ModelRetry, match="no direct messages"):
        await send(cast(RunContext[Any], None), segments, "dm")

    # `here` is unaffected — only the destination that cannot land is refused.
    assert await send(cast(RunContext[Any], None), segments, "here")


async def test_send_to_dm_from_a_dm_is_not_refused() -> None:
    # Being there already stops a `scheme` — nowhere to move the conversation to —
    # but not a send: "send that to me" from a DM means the place it is already in.
    capability = _gate(already_private=True)
    assert capability.private_blocked_by == "already_private"
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, "dm")

    assert result.metadata == [MessageSentEvent(segments=segments, destination="dm")]
