"""The MCP send tool publishes once through the core nerve and records delivery."""

from __future__ import annotations

import json
from typing import ClassVar, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.config import ChannelConfig
from octomate.managers.gateway import OctomateSession
from octomate.managers.user import UserManager
from octomate.nerve import ChannelTopic, Nerve
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.triage import DIRECT_TARGET, DirectTarget
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.channel import ChannelSurfaces
from octomate.types.json import JsonObject
from tests.support.agents import FakeAgent, ScriptedStream, ScriptedTurn
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import FakeThreadManager, a_workspace_manager
from tests.support.mcp import core_tool_capabilities, core_tool_server, gateway_schemas
from tests.support.nerve import held
from tests.support.users import a_user


class _NoDmChannel(FakeChannelTentacle):
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(sub_thread=True)


def _gate(
    *,
    direct_messages: bool = True,
    already_private: bool = False,
    chat_type: ChatType | None = None,
) -> OctomateSession:
    """A routing-only gate: on a channel with direct messages unless asked
    otherwise, answering from a group unless asked to be in the DM already.

    `chat_type` overrides the type the surface reports without changing whether it
    is private — an assistant pane is a thread only one person can read."""
    threads = FakeThreadManager()
    return OctomateSession(
        nerve=Nerve(),
        channel_routes={},
        current_agent_id="inkling",
        channels={"im": FakeChannelTentacle() if direct_messages else _NoDmChannel()},
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type=chat_type or ("dm" if already_private else "group"),
            chat_id="room",
            user_id="alice",
            shared=not already_private,
        ),
        users=threads.users,
        threads=threads,
        workspaces=a_workspace_manager(),
    )


async def test_send_returns_sent_and_announces_event() -> None:
    session = _gate()
    threads = FakeThreadManager()
    assert session.nerve is not None
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session, threads=threads)) as client,
    ):
        result = await client.call_tool(
            "gateway_send",
            {"segments": [{"type": "markdown", "data": {"text": "halfway there"}}]},
        )
        [sent] = [impulse.event for impulse in held(events)]
    assert result.data == "sent"
    assert isinstance(sent, MessageSentEvent)
    assert sent.segments == [MarkdownSegment(data={"text": "halfway there"})]
    assert sent.destination == session.conversation_address
    assert len(threads.outbounds) == 1
    assert sent.thread_message_id == threads.outbounds[0].id


async def test_send_tool_exposes_no_channel_fields() -> None:
    schema = (await gateway_schemas(_gate()))["send"]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"segments", "destination"}
    destination = TypeAdapter(JsonObject).validate_python(properties["destination"])
    variants = TypeAdapter(list[JsonObject]).validate_python(destination["oneOf"])
    kinds = []
    for variant in variants:
        fields = TypeAdapter(dict[str, JsonObject]).validate_python(
            variant["properties"]
        )
        kinds.append(fields["kind"]["const"])
    assert set(kinds) == {"dm", "here"}
    rendered = json.dumps(schema)
    for runtime_state in ("im", "alice", "room", "lark"):
        assert f'"{runtime_state}"' not in rendered


async def test_send_carries_the_named_destination() -> None:
    session = _gate()
    assert session.nerve is not None
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session)) as client,
    ):
        await client.call_tool(
            "gateway_send",
            {
                "segments": [{"type": "markdown", "data": {"text": "summary"}}],
                "destination": {"kind": "dm"},
            },
        )
        [sent] = [impulse.event for impulse in held(events)]
    assert isinstance(sent, MessageSentEvent)
    assert sent.destination is not None
    assert sent.destination.chat_type == "dm"
    assert sent.destination.user_id == "alice"


async def test_inkling_agent_send_tool_publishes_once_end_to_end() -> None:
    session = _gate()
    assert session.nerve is not None
    agent = Agent(
        TestModel(), deps_type=type(None), capabilities=core_tool_capabilities(session)
    )
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
    async with session.nerve.subscribe(ChannelTopic("im")) as events:
        result = await agent.run(
            "send something",
            conversation_id=str(uuid7()),
            model=FunctionModel(stream_function=script, model_name="scripted"),
        )
        [sent] = [impulse.event for impulse in held(events)]
    assert result.output == "done"
    assert isinstance(sent, MessageSentEvent)
    assert [str(segment) for segment in sent.segments] == ["progress"]


async def test_send_to_dm_is_refused_where_there_are_none() -> None:
    session = _gate(direct_messages=False)
    async with Client(core_tool_server(session)) as client:
        with pytest.raises(ToolError, match="no direct messages"):
            await client.call_tool(
                "gateway_send", {"segments": [], "destination": {"kind": "dm"}}
            )
        assert (await client.call_tool("gateway_send", {"segments": []})).data == "sent"


@pytest.mark.parametrize("chat_type", ["dm", "thread"])
async def test_send_to_dm_from_a_private_surface_stays_here(
    chat_type: ChatType,
) -> None:
    session = _gate(already_private=True, chat_type=chat_type)
    assert session.private_blocked_by == "already_private"
    assert await session.resolve_send(DIRECT_TARGET) is None
    assert session.nerve is not None
    async with (
        session.nerve.subscribe(ChannelTopic("im")) as events,
        Client(core_tool_server(session)) as client,
    ):
        await client.call_tool(
            "gateway_send", {"segments": [], "destination": {"kind": "dm"}}
        )
        [sent] = [impulse.event for impulse in held(events)]
    assert isinstance(sent, MessageSentEvent)
    assert sent.destination == session.conversation_address


async def test_send_reaches_another_channel_the_asker_is_registered_on(
    in_memory_engine: None,
) -> None:
    await a_user("luhui", profiles={"im": "alice", "lark": "ou_alice"})
    users = UserManager()
    session = _gate()
    session.users = users
    session.user_profile = await users.ensure_profile(
        "im", UserProfile(channel_user_id="alice")
    )
    session.channels["lark"] = FakeChannelTentacle(
        "lark", config=ChannelConfig(type="fake", agents=["other"])
    )
    session.destination_cache = []
    assert await session.resolve_send(DirectTarget(channel="lark")) == ChannelAddress(
        "lark", "dm", "", "ou_alice"
    )
    assert session.nerve is not None
    async with (
        session.nerve.subscribe(ChannelTopic("lark")) as events,
        Client(core_tool_server(session)) as client,
    ):
        await client.call_tool(
            "gateway_send",
            {"segments": [], "destination": {"kind": "dm", "channel": "lark"}},
        )
        [sent] = [impulse.event for impulse in held(events)]
        with pytest.raises(ToolError, match="No connected channel 'napcat'"):
            await client.call_tool(
                "gateway_send",
                {"segments": [], "destination": {"kind": "dm", "channel": "napcat"}},
            )
    assert isinstance(sent, MessageSentEvent)
    assert sent.destination is not None
    assert sent.destination.channel_tentacle_id == "lark"
    assert sent.destination.user_id == "ou_alice"


async def test_inspect_reveals_where_else_the_asker_can_be_reached() -> None:
    lark = ChannelAddress("lark", "dm", "", "ou_123")
    session = _gate()
    session.destination_cache = [lark]
    assert lark in await session.inspect("destinations")


async def test_the_gate_works_out_where_else_the_asker_is(
    in_memory_engine: None,
) -> None:
    """The computation the gate now owns: registry link → reachable destination.

    Only channels that are connected, have direct messages, and serve an agent —
    somewhere nobody could answer from is not somewhere this can go.
    """
    await a_user(
        "luhui", profiles={"im": "alice", "lark": "ou_alice", "mute": "m_alice"}
    )
    users = UserManager()
    here = await users.ensure_profile("im", UserProfile(channel_user_id="alice"))

    lark = FakeChannelTentacle(
        id="lark",
        config=ChannelConfig(type="fake", agents=["other"]),
    )
    # Routes only to an agent this gate does not have, so it is not offered
    # however reachable it looks.
    mute = FakeChannelTentacle(
        id="mute",
        config=ChannelConfig(type="fake", agents=["absent"]),
    )

    session = _gate()
    session.users = users
    session.user_profile = here
    session.channels = {"im": FakeChannelTentacle(), "lark": lark, "mute": mute}
    session.agents = {"other": cast(AgentTentacle, FakeAgent(id="other"))}

    assert await session.destinations() == [
        ChannelAddress("im", "dm", "", "alice"),
        ChannelAddress("lark", "dm", "", "ou_alice"),
    ]
    # Computed once and kept: a gateway lasts one turn.
    assert await session.destinations() is await session.destinations()
