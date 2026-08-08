"""The gate's `send` tool: returns "sent" and announces a MessageSentEvent
on the run's event stream, which the consumer rendering the run delivers. The tool
names `here` or `dm` and resolves neither — but the gate it lives on knows the
channel's surfaces, so an unreachable `dm` is refused rather than redirected.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

import pytest
from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.ask import AskCapability
from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.capabilities.todos import TodoCapability
from octomate.config import AgentModelConfig, ChannelConfig
from octomate.config.users import UserConfig
from octomate.managers.user import UserManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment, MessageSegment
from octomate.schemas.triage import Destination, Scrying
from octomate.schemas.user import UserProfile
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channels.base import ChannelSurfaces
from tests.support.agents import ScriptedStream, ScriptedTurn
from tests.support.channels import FakeChannelTentacle


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
            chat_type="dm" if already_private else "group",
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
    # A plain string, never an enum of the surfaces this run can reach: that list is
    # per-user, and the tool block is a cached prompt segment. `scry` carries it.
    assert properties["destination"]["type"] == "string"
    assert "enum" not in properties["destination"]


async def test_send_carries_the_named_destination() -> None:
    capability = _gate()
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, "dm")

    # The event carries the *resolved* surface, so the consumer addresses it without
    # deciding anything and a refused surface can never reach one.
    assert result.metadata == [
        MessageSentEvent(
            segments=segments,
            destination=ChannelAddress(
                channel_tentacle_id="im",
                chat_type="dm",
                chat_id="",
                user_id="alice",
            ),
        )
    ]


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

    # The event carries the *resolved* surface, so the consumer addresses it without
    # deciding anything and a refused surface can never reach one.
    # Resolved to nowhere else, so it simply lands here — not refused.
    assert result.metadata == [MessageSentEvent(segments=segments, destination=None)]


async def test_send_reaches_another_channel_the_asker_is_registered_on() -> None:
    # The cross-channel case: the model names a channel and nothing else. Who is
    # fixed — whoever asked — and their account there came from the identity
    # registry when the gate was built, so no user id ever reaches the tool args.
    lark = Destination(
        handle="lark",
        label="their direct messages on Lark",
        address=ChannelAddress(
            channel_tentacle_id="lark",
            chat_type="dm",
            chat_id="",
            user_id="ou_alice",
        ),
    )
    capability = _gate()
    # Seeded rather than computed: this is about resolving a handle, not about
    # reaching the identity registry, which `test_user` covers.
    capability.computed_destinations = [*capability.built_in_destinations, lark]
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, "lark")

    assert result.metadata == [
        MessageSentEvent(segments=segments, destination=lark.address)
    ]

    # A channel it was never told about is refused, with the list it could have used.
    with pytest.raises(ModelRetry, match="No such destination 'napcat'"):
        await send(cast(RunContext[Any], None), segments, "napcat")


async def test_scry_reveals_where_else_the_asker_can_be_reached() -> None:
    # The only carrier for a per-user list: a tool result. In the schema it would
    # fork the cached tool block, in the instructions the cached system block.
    lark = Destination(
        handle="lark",
        label="their direct messages on Lark",
        address=ChannelAddress(
            channel_tentacle_id="lark",
            chat_type="dm",
            chat_id="",
            user_id="ou_alice",
        ),
    )
    capability = _gate()
    # Seeded rather than computed: this is about resolving a handle, not about
    # reaching the identity registry, which `test_user` covers.
    capability.computed_destinations = [*capability.built_in_destinations, lark]
    assert capability.toolset is not None
    scry = capability.toolset.tools["scry"].function

    scrying = await scry(cast(RunContext[Any], None))

    assert lark in scrying.destinations
    assert "lark" in str(scrying)


async def test_scry_does_not_file_this_conversation_as_somewhere_else() -> None:
    # `scry` is the model's only view of where it can go, so the heading has to be
    # true of every row under it: this chat is neither remote nor private.
    capability = _gate()
    scrying = Scrying(routes=[], destinations=await capability.destinations())

    # `thread` is a place in *this* chat, so a heading promising somewhere else, or
    # somewhere private, would be false of it.
    assert "thread" in [one.handle for one in scrying.destinations]
    assert "privately" not in str(scrying)
    assert "Where else" not in str(scrying)
    assert "Where you can put this:" in str(scrying)


async def test_the_gate_works_out_where_else_the_asker_is(
    in_memory_engine: None,
) -> None:
    """The computation the gate now owns: registry link → reachable destination.

    Only channels that are connected, have direct messages, and serve an agent —
    somewhere nobody could answer from is not somewhere this can go.
    """
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "profiles": {
                        "im": {"channel_user_id": "alice"},
                        "lark": {"channel_user_id": "ou_alice"},
                        "mute": {"channel_user_id": "m_alice"},
                    }
                }
            )
        }
    )
    await users.reconcile()
    here = await users.ensure_profile("im", UserProfile(channel_user_id="alice"))

    lark = FakeChannelTentacle(
        id="lark",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="other", model="test")]
        ),
    )
    # Serves nobody, so it is not offered however reachable it looks.
    mute = FakeChannelTentacle(id="mute", config=ChannelConfig(type="fake", agents=[]))

    capability = _gate()
    capability.users = users
    capability.user_profile = here
    capability.channels = {"im": FakeChannelTentacle(), "lark": lark, "mute": mute}
    capability.agents = {"other": cast(Any, object())}

    assert [one.handle for one in await capability.linked_destinations()] == ["lark"]
    # Computed once and kept: a gate lasts one turn.
    assert await capability.destinations() is await capability.destinations()
