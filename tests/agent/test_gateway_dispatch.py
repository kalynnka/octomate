from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import NotRequired, TypedDict
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.gateway import GatewayCapability
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.managers.gateway import GatewayRefusal, OctomateSession
from octomate.mcp.gateway import native_session
from octomate.mcp.server import octomate_mcp
from octomate.reflex.nodes.summon import Summon
from octomate.schemas.awakes import SummonSignal, UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelSurfaces
from octomate.tentacles.claude.mcp import sdk_tool
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project, a_registry, fixed_session
from tests.support.nerve import awake_on, drawing, kick_and_wait
from tests.support.users import a_user


class SummonArguments(TypedDict):
    agent_id: str
    model: str
    hint: str
    reason: str
    brief: str
    effort: NotRequired[str]
    project: NotRequired[str]
    ref: NotRequired[str]


ARGUMENTS: SummonArguments = {
    "agent_id": "claude",
    "model": "opus",
    "hint": "Investigating the failing test",
    "reason": "needs coding",
    "brief": "Reproduce the failing test and explain the cause.",
    "effort": "high",
}


@pytest.fixture
async def summon_session(
    in_memory_engine: AsyncEngine,
) -> tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent]:
    host = Octomate()
    channel = host.connect(
        FakeChannelTentacle(
            config=ChannelConfig(
                type="fake",
                agents=["inkling", "claude"],
                stream=ChannelStreamConfig(enabled=False),
            )
        )
    )
    caller = host.connect(FakeAgent(id="inkling"))
    receiver = host.connect(FakeAgent(id="claude", reception_output="Found the cause"))
    await a_user("someone", profiles={"im": "alice"})
    profile = await host.users.profile("im", "alice")
    assert profile is not None
    address = ChannelAddress("im", "group", "room", "alice", shared=True)
    thread = await host.threads.ensure(address)
    conversation = await host.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    await host.threads.record_handoff(
        thread, to_agent_tentacle_id="inkling", to_model="test"
    )
    session = OctomateSession(
        channel_routes=host.gateway.available_routes(host.channels, host.agents),
        current_agent_id="inkling",
        channels=host.channels,
        users=host.users,
        user_profile=profile,
        agents=host.agents,
        thread_id=thread.id,
        conversation_id=conversation.id,
        conversation_address=address,
        threads=host.threads,
        workspaces=host.workspaces,
    )
    return session, channel, caller, receiver


async def test_summon_kicks_a_peer_thread_and_keeps_the_source_running(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
) -> None:
    session, channel, caller, receiver = summon_session
    host = channel.octomate
    source = session.conversation_address
    assert source is not None
    assert session.thread_id is not None
    assert session.user_profile is not None
    async with drawing(channel), awake_on(host, source):
        opened = await session.summon(**ARGUMENTS, destination=source)
        assert isinstance(opened, ChannelAddress)
        task = host.awakes[ThreadKey.from_address(opened)]
        assert not task.done()
        assert ThreadKey.from_address(source) in host.awakes
        assert opened != source
        assert session.conversation_address == source
        assert session.decision is None
        await task

    thread = await host.threads.get(ThreadKey.from_address(opened))
    assert thread is not None
    assert thread.id != session.thread_id
    assert thread.active_agent_tentacle_id == "claude"
    assert len(thread.handoffs) == 1
    handoff = thread.handoffs[0]
    assert handoff.source_agent_tentacle_id == "inkling"
    assert handoff.source_conversation_id == session.conversation_id
    assert handoff.brief == ARGUMENTS["brief"]
    [conversation] = thread.conversations
    assert conversation.subagent_id == ""
    opener = thread.messages[0]
    assert opener.actor_kind == "system"
    assert opener.message_text == ARGUMENTS["hint"]
    assert opener.sender_id == session.user_profile.id
    assert any(message.message_text == "Found the cause" for message in thread.messages)
    [turn] = receiver.turns
    assert turn.prompt == ARGUMENTS["brief"]
    assert turn.history == []
    assert turn.effort == "high"
    assert turn.address == opened
    assert caller.turns == []
    assert channel.sub_threads == [(source, ARGUMENTS["hint"])]

    event = MessageEvent(
        tentacle_id="im",
        chat_type="group",
        chat_id="room",
        user_id="alice",
        sender=session.user_profile,
        shared=True,
        segments=[TextSegment(data={"text": "Continue planning here"})],
    )
    async with drawing(channel):
        await kick_and_wait(host, UserMessageSignal([event]))
    original = await host.threads.get(session.thread_id)
    assert original is not None
    assert original.active_agent_tentacle_id == "inkling"
    assert len(original.handoffs) == 1
    assert len(caller.turns) == 1
    assert len(receiver.turns) == 1


async def test_independent_summon_initializes_without_entering_the_handoff_node(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, channel, _caller, _receiver = summon_session
    host = channel.octomate
    threads = host.threads
    kick = AsyncMock()
    with monkeypatch.context() as patch:
        patch.setattr(host, "kick", kick)
        for name in ("threads", "conversations", "workspaces"):
            patch.delattr(host, name)
        opened = await session.summon(
            **ARGUMENTS, destination=session.conversation_address
        )

    assert isinstance(opened, ChannelAddress)
    kick.assert_awaited_once()
    signal = kick.call_args.args[0]
    assert isinstance(signal, SummonSignal)
    assert signal.destination == opened
    assert await threads.get(ThreadKey.from_address(opened)) is None
    handoff = AsyncMock(side_effect=AssertionError("Independent work entered Summon."))
    monkeypatch.setattr(Summon, "run", handoff)
    async with drawing(channel):
        await kick_and_wait(host, signal)
    handoff.assert_not_awaited()
    thread = await threads.get(ThreadKey.from_address(opened))
    assert thread is not None
    assert thread.key == ThreadKey.from_address(opened)
    assert len(thread.conversations) == 1
    assert thread.handoffs[0].brief == ARGUMENTS["brief"]
    assert thread.messages[0].message_text == ARGUMENTS["hint"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"agent_id": "missing"}, "Invalid summon route"),
        ({"agent_id": "inkling"}, "Invalid summon route"),
        ({"model": "missing"}, "Invalid summon route"),
        ({"effort": "impossible"}, "does not accept effort"),
        ({"ref": "main"}, "requires a project"),
        ({"project": "missing"}, "No project called"),
    ],
)
async def test_summon_refuses_invalid_work_before_creating_a_thread(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    change: dict[str, str],
    message: str,
) -> None:
    session, channel, _caller, receiver = summon_session
    arguments = TypeAdapter(SummonArguments).validate_python({**ARGUMENTS, **change})
    with pytest.raises(GatewayRefusal, match=message):
        await session.summon(**arguments, destination=session.conversation_address)
    assert channel.sub_threads == []
    assert receiver.turns == []
    assert session.decision is None


@pytest.mark.parametrize("surface", ["nested", "unsupported"])
async def test_summon_refuses_surfaces_without_an_independent_thread(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    session, channel, _caller, _receiver = summon_session
    assert session.conversation_address is not None
    if surface == "nested":
        session.conversation_address = replace(
            session.conversation_address,
            chat_type="thread",
            channel_thread_id="existing",
        )
    else:
        monkeypatch.setattr(channel, "surfaces", ChannelSurfaces(direct_message=True))
    with pytest.raises(GatewayRefusal):
        await session.summon(**ARGUMENTS, destination=session.conversation_address)
    assert channel.sub_threads == []
    assert session.decision is None


async def test_summon_reports_creation_failure_without_kicking_here(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, channel, _caller, _receiver = summon_session
    assert session.conversation_address is not None
    monkeypatch.setattr(
        channel,
        "start_sub_thread",
        AsyncMock(return_value=session.conversation_address),
    )
    with pytest.raises(GatewayRefusal, match="could not create a thread"):
        await session.summon(**ARGUMENTS, destination=session.conversation_address)
    assert channel.octomate.awakes == {}


async def test_summon_validates_project_access_and_ref_before_binding(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    tmp_path: Path,
) -> None:
    session, channel, _caller, receiver = summon_session
    host = channel.octomate
    root = tmp_path / "project"
    root.mkdir()
    (root / "task.txt").write_text("project work")
    host.workspaces.projects = await a_registry(a_project(root))
    profile = session.user_profile
    assert profile is not None
    assert session.conversation_address is not None
    arguments: SummonArguments = {**ARGUMENTS, "project": "project"}
    session.user_profile = UserProfile(
        channel_tentacle_id="im", channel_user_id="visitor"
    )
    session.conversation_address = replace(
        session.conversation_address, user_id="visitor"
    )
    with pytest.raises(GatewayRefusal, match="no registered user"):
        await session.summon(**arguments, destination=session.conversation_address)
    session.user_profile = profile
    assert session.conversation_address is not None
    session.conversation_address = replace(
        session.conversation_address, user_id="alice"
    )
    arguments["ref"] = "missing"
    with pytest.raises(GatewayRefusal, match="no 'missing' to start from"):
        await session.summon(**arguments, destination=session.conversation_address)
    assert channel.sub_threads == []
    del arguments["ref"]

    async with drawing(channel):
        opened = await session.summon(
            **arguments, destination=session.conversation_address
        )
        assert isinstance(opened, ChannelAddress)
        await host.awakes[ThreadKey.from_address(opened)]
        thread = await host.threads.get(ThreadKey.from_address(opened))
        assert thread is not None
        assert thread.project_id is not None
        workspace = host.workspaces.existing(thread.id)
        assert workspace is not None
        assert (workspace / "task.txt").read_text() == "project work"
    assert len(receiver.turns) == 1


@pytest.mark.parametrize("projection", ["mcp", "claude"])
async def test_summon_is_exposed_with_the_same_schema_and_result(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    projection: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, channel, _caller, _receiver = summon_session
    assert session.conversation_address is not None
    opened = replace(
        session.conversation_address, chat_type="thread", channel_thread_id="new"
    )
    summon = AsyncMock(return_value=opened)
    monkeypatch.setattr(session, "summon", summon)
    host = channel.octomate
    resolve_session = AsyncMock(return_value=session)
    server = octomate_mcp(resolve_session, host.threads, manager=host.mcp)
    tool = await server.get_tool("gateway_summon")
    assert tool is not None
    resolve_session.assert_not_awaited()
    capability = GatewayCapability(session=session)
    assert capability.toolset is not None
    schema = capability.toolset.tools["summon"].tool_def.parameters_json_schema
    assert tool.parameters["required"] == schema["required"]
    for name, parameter in schema["properties"].items():
        assert tool.parameters["properties"][name] == {
            key: value for key, value in parameter.items() if key != "description"
        }
    if projection == "mcp":
        async with Client(server) as client:
            await client.list_tools()
            resolve_session.assert_not_awaited()
            result = await client.call_tool("gateway_summon", dict(ARGUMENTS))
        structured = result.structured_content
        assert structured is not None
        address = structured["result"]
        assert TypeAdapter(ChannelAddress).validate_python(address) == opened
    else:
        sdk = sdk_tool(tool)
        payload = TypeAdapter(dict[str, JsonValue]).validate_python(ARGUMENTS)
        sdk_result = await sdk.handler(payload)
        assert sdk_result.get("is_error") is not True
        content = sdk_result["content"]
        assert isinstance(content, list)
        block = content[0]
        assert isinstance(block, dict)
        text = block["text"]
        assert isinstance(text, str)
        assert TypeAdapter(ChannelAddress).validate_json(text) == opened
    summon.assert_awaited_once()
    assert summon.await_args is not None
    assert summon.await_args.kwargs == {
        **ARGUMENTS,
        "destination": None,
        "project": None,
        "ref": None,
    }
    resolve_session.assert_awaited_once()


async def test_native_summon_starts_work_without_a_source_thread(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
) -> None:
    _session, channel, _caller, receiver = summon_session
    host = channel.octomate
    session = await native_session(host, CLAUDE_NATIVE_ID, "someone")
    server = octomate_mcp(fixed_session(session), host.threads, manager=host.mcp)
    async with drawing(channel), Client(server) as client:
        assert "gateway_summon" in {tool.name for tool in await client.list_tools()}
        with pytest.raises(ToolError, match="A native session cannot be handed over"):
            await client.call_tool("gateway_summon", dict(ARGUMENTS))
        assert channel.sub_threads == []
        result = await client.call_tool(
            "gateway_summon",
            {
                **ARGUMENTS,
                "destination": {
                    "channel_tentacle_id": "im",
                    "chat_type": "dm",
                    "chat_id": "",
                    "user_id": "alice",
                },
            },
        )
        structured = result.structured_content
        assert structured is not None
        address = structured["result"]
        opened = TypeAdapter(ChannelAddress).validate_python(address)
        await host.awakes[ThreadKey.from_address(opened)]
    assert session.thread_id is None
    assert session.conversation_id is None
    assert session.conversation_address is None
    assert session.decision is None
    thread = await host.threads.get(ThreadKey.from_address(opened))
    assert thread is not None
    assert thread.active_agent_tentacle_id == "claude"
    [handoff] = thread.handoffs
    assert handoff.source_agent_tentacle_id == CLAUDE_NATIVE_ID
    assert handoff.source_conversation_id is None
    profile = await host.users.profile("im", "alice")
    assert profile is not None
    assert thread.messages[0].sender_id == profile.id
    assert receiver.turns[0].prompt == ARGUMENTS["brief"]
    capability = GatewayCapability(session=session)
    assert capability.toolset is not None
    assert "summon" in capability.toolset.tools


@pytest.mark.parametrize("user_id", ["alice", "someone-else"])
async def test_native_summon_validates_linked_identity_before_opening(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
    user_id: str,
) -> None:
    _session, channel, _caller, _receiver = summon_session
    host = channel.octomate
    await a_user("unlinked")
    username = "unlinked" if user_id == "alice" else "someone"
    session = await native_session(host, CLAUDE_NATIVE_ID, username)
    with pytest.raises(GatewayRefusal, match=r"Link your profile|does not belong"):
        await session.summon(
            **ARGUMENTS, destination=ChannelAddress("im", "dm", "", user_id)
        )
    assert channel.sub_threads == []


async def test_native_summon_attributes_a_console_thread_to_its_owner(
    summon_session: tuple[OctomateSession, FakeChannelTentacle, FakeAgent, FakeAgent],
) -> None:
    _session, channel, _caller, receiver = summon_session
    host = channel.octomate
    console = host.connect(
        TrunklineTentacle("web", host, config=TrunklineChannelConfig(agents=["claude"]))
    )
    console.self_profile = await console.ink.inspect()
    session = await native_session(host, CLAUDE_NATIVE_ID, "someone")
    assert session.user_profile is not None
    user_id = session.user_profile.user_id
    assert user_id is not None
    opened = await session.summon(
        **ARGUMENTS,
        destination=ChannelAddress("web", "thread", str(user_id), str(user_id)),
    )
    assert isinstance(opened, ChannelAddress)
    await host.awakes[ThreadKey.from_address(opened)]
    thread = await host.threads.get(ThreadKey.from_address(opened), user_id=user_id)
    assert thread is not None
    profile = await host.users.profile("web", str(user_id))
    assert profile is not None
    assert profile.user_id == user_id
    assert thread.messages[0].sender_id == profile.id
    assert receiver.turns[0].address == opened
