"""The gate's `send` tool: returns "sent" and announces a MessageSentEvent
on the run's event stream, which the consumer rendering the run delivers. The tool
names `here` or `dm` and resolves neither — but the gate it lives on knows the
channel's surfaces, so an unreachable `dm` is refused rather than redirected.
"""

from __future__ import annotations

import inspect
import json
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
from octomate.managers.gateway import GatewaySession
from octomate.managers.user import UserManager
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.segments import MarkdownSegment, MessageSegment
from octomate.schemas.triage import (
    DIRECT_TARGET,
    HERE_TARGET,
    ChannelTarget,
    Destination,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelSurfaces
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.inkling.prompts import SYSTEM_PROMPT
from tests.support.agents import ScriptedStream, ScriptedTurn
from tests.support.channels import FakeChannelTentacle


class _NoDmChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)


def _gate(
    *,
    direct_messages: bool = True,
    already_private: bool = False,
    chat_type: ChatType | None = None,
) -> GatewayCapability:
    """A routing-only gate: on a channel with direct messages unless asked
    otherwise, answering from a group unless asked to be in the DM already.

    `chat_type` overrides the type the surface reports without changing whether it
    is private — an assistant pane is a thread only one person can read."""
    return GatewayCapability(
        session=GatewaySession(
            channel_routes={},
            current_agent_id="inkling",
            channels={
                "im": FakeChannelTentacle() if direct_messages else _NoDmChannel()
            },
            conversation_address=ChannelAddress(
                channel_tentacle_id="im",
                chat_type=chat_type or ("dm" if already_private else "group"),
                chat_id="room",
                user_id="alice",
                shared=not already_private,
            ),
        )
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


def _destination_kinds(schema: dict[str, object]) -> list[str]:
    """The `kind` each `destination` variant declares, dug out of the refs the union
    generates. What matters is that the set is closed and named in the schema."""
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    kinds: list[str] = []
    for name, definition in defs.items():
        if not name.endswith("Target") or not isinstance(definition, dict):
            continue
        properties = definition["properties"]
        assert isinstance(properties, dict)
        kinds.append(str(properties["kind"]["const"]))
    return kinds


def test_send_tool_exposes_no_channel_fields() -> None:
    # `destination` names one of a closed set; no channel, chat or user id ever
    # reaches the tool, which is what keeps the schema constant across runs.
    capability = _gate()
    assert capability.toolset is not None
    tool = capability.toolset.tools["send"]
    params = set(inspect.signature(tool.function).parameters)
    assert params == {"ctx", "segments", "destination"}

    # A closed set of shapes — one per kind of place — and the only free text in it
    # is a channel id the model copies from `scry`. Which channels *this* person is
    # on never reaches the schema: that list is per-user and the tool block is a
    # cached prompt segment, so it would fork the prefix at the front.
    schema = tool.tool_def.parameters_json_schema
    assert isinstance(schema, dict)
    assert sorted(_destination_kinds(schema)) == ["channel", "dm", "here"]
    # And no id from this run reaches any of it.
    rendered = json.dumps(schema)
    for runtime_state in ("im", "alice", "room", "lark"):
        assert f'"{runtime_state}"' not in rendered


async def test_send_carries_the_named_destination() -> None:
    capability = _gate()
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, DIRECT_TARGET)

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
        await send(cast(RunContext[Any], None), segments, DIRECT_TARGET)

    # `here` is unaffected — only the destination that cannot land is refused.
    assert await send(cast(RunContext[Any], None), segments, HERE_TARGET)


async def test_send_to_dm_from_a_dm_is_not_refused() -> None:
    # Being there already stops a `scheme` — nowhere to move the conversation to —
    # but not a send: "send that to me" from a DM means the place it is already in.
    capability = _gate(already_private=True)
    assert capability.session.private_blocked_by == "already_private"
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, DIRECT_TARGET)

    # The event carries the *resolved* surface, so the consumer addresses it without
    # deciding anything and a refused surface can never reach one.
    # Resolved to nowhere else, so it simply lands here — not refused.
    assert result.metadata == [MessageSentEvent(segments=segments, destination=None)]


async def test_send_to_dm_from_a_private_thread_is_not_refused() -> None:
    # The same rule one surface further in: a Slack assistant pane is a thread by
    # type and private in fact, so `dm` there names the pane rather than a second
    # surface beside it. Resolving one would post outside the chat being had.
    capability = _gate(already_private=True, chat_type="thread")
    assert capability.session.private_blocked_by == "already_private"
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(cast(RunContext[Any], None), segments, DIRECT_TARGET)

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
    capability.session.computed_destinations = [
        *capability.session.built_in_destinations,
        lark,
    ]
    assert capability.toolset is not None
    send = capability.toolset.tools["send"].function
    segments: list[MessageSegment] = [MarkdownSegment(data={"text": "the summary"})]

    result = await send(
        cast(RunContext[Any], None), segments, ChannelTarget(channel="lark")
    )

    assert result.metadata == [
        MessageSentEvent(segments=segments, destination=lark.address)
    ]

    # A channel it was never told about is refused, with the list it could have used.
    with pytest.raises(ModelRetry, match="No such destination 'napcat'"):
        await send(
            cast(RunContext[Any], None), segments, ChannelTarget(channel="napcat")
        )


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
    capability.session.computed_destinations = [
        *capability.session.built_in_destinations,
        lark,
    ]
    assert capability.toolset is not None
    scry = capability.toolset.tools["scry"].function

    places = await scry(cast(RunContext[Any], None), "destinations")

    assert lark in places


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
    # Routes only to an agent this gate does not have, so it is not offered
    # however reachable it looks.
    mute = FakeChannelTentacle(
        id="mute",
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="absent", model="test")]
        ),
    )

    session = _gate().session
    session.users = users
    session.user_profile = here
    session.channels = {"im": FakeChannelTentacle(), "lark": lark, "mute": mute}
    session.agents = {"other": cast(Any, object())}

    assert [one.handle for one in await session.linked_destinations()] == ["lark"]
    # Computed once and kept: a gateway lasts one turn.
    assert await session.destinations() is await session.destinations()
