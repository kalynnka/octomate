"""Authenticated thread actions share gateway policy and execute the real graph."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.auth import current_user
from octomate.config import ChannelConfig, DiscordChannelConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.managers.gateway import OctomateSession
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import CODEX_NATIVE_ID, Thread, ThreadKey
from octomate.schemas.triage import ChannelTarget
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.discord.base import DiscordTentacle
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.tentacles.trunkline.base import TrunklineDirective
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user


@dataclass
class Case:
    app: Octomate
    thread: Thread
    owner: User
    agent: FakeAgent
    receiver: FakeAgent
    far: FakeChannelTentacle
    channel: TrunklineTentacle


@pytest.fixture
async def case(in_memory_engine: AsyncEngine, tmp_path: Path) -> Case:
    app = Octomate(workspaces=WorkspaceManager(workspaces_dir=tmp_path / "workspaces"))
    owner = await a_user(profiles={"far": "alice"})
    app.dependency_overrides[current_user] = lambda: owner
    agent = app.connect(FakeAgent(id="first", allow_reception_run=True))
    receiver = app.connect(FakeAgent(id="second", allow_reception_run=True))
    channel = app.connect(
        TrunklineTentacle(
            "trunkline", app, config=TrunklineChannelConfig(agents=[agent.id])
        )
    )
    far = app.connect(
        FakeChannelTentacle(
            "far",
            app,
            config=ChannelConfig(type="fake", agents=[agent.id, receiver.id]),
        )
    )
    await channel.probe()
    await far.probe()
    thread = await app.thread_manager.ensure(
        ThreadKey("trunkline", "thread", str(owner.id), uuid7().hex)
    )
    await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    await app.thread_manager.record_outbound(
        thread,
        agent_tentacle_id=agent.id,
        segments=[TextSegment(data={"text": "Original history"})],
        sender=UserProfile(channel_user_id=str(owner.id), user_id=owner.id),
    )
    return Case(app, thread, owner, agent, receiver, far, channel)


@pytest.fixture
async def client(case: Case) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app),
        base_url="http://test",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        yield client


async def test_options_and_summon_here(case: Case, client: httpx.AsyncClient) -> None:
    path = f"/api/trunkline/threads/{case.thread.id}"
    options = await client.get(f"{path}/operations")
    assert options.status_code == 200, options.text
    data = options.json()
    assert [one["target"] for one in data["teleport"]["destinations"]] == [
        {"kind": "channel", "channel": "trunkline"},
        {"kind": "channel", "channel": "far"},
    ]
    assert [one["target"]["kind"] for one in data["summon"]["destinations"]] == [
        "here",
        "channel",
        "channel",
    ]
    assert all(
        route["agent_id"] == "second"
        for dest in data["summon"]["destinations"]
        for route in dest["routes"]
    )
    response = await client.post(
        f"{path}/summon",
        json={
            "destination": {"kind": "here"},
            "agent_id": "second",
            "model": "test",
            "brief": "Investigate the existing work",
            "hint": "Changing agent",
        },
    )
    assert response.status_code == 200, response.text
    assert '"event_kind":"gateway"' in response.text, response.text
    assert "run_error" not in response.text, response.text
    assert case.receiver.streams[-1].prompt == "Investigate the existing work"
    updated = await case.app.thread_manager.get(case.thread.id)
    assert updated is not None
    assert updated.active_agent_tentacle_id == "second"
    assert updated.latest_handoff is not None
    assert updated.latest_handoff.source_conversation_id is not None


async def test_teleport_creates_independent_owned_destination(
    case: Case, client: httpx.AsyncClient
) -> None:
    source = (await case.app.conversations.for_thread(case.thread.id))[0]
    case.agent.models["opus"] = "picked-model"
    await case.app.conversations.record_agent_run(
        source,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("Keep this history")]),
            ModelResponse(parts=[TextPart("Understood")], model_name="opus"),
        ],
        model_name="opus",
        permission_mode="default",
    )
    await case.app.conversations.set_permission_mode(source, "default")
    response = await client.post(
        f"/api/trunkline/threads/{case.thread.id}/teleport",
        json={
            "destination": {"kind": "channel", "channel": "far"},
            "hint": "Work here",
        },
    )
    assert response.status_code == 200
    assert "run_error" not in response.text, response.text
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    event = next(frame for frame in frames if frame["event_kind"] == "gateway")
    assert event["destination"]["channel_tentacle_id"] == "far"
    assert case.agent.turns[-1].deferred_results is None
    assert "Current channel address:" in str(case.agent.turns[-1].prompt)
    threads = await case.app.thread_manager.list_threads(user_id=case.owner.id)
    landed = next(thread for thread in threads if thread.channel_tentacle_id == "far")
    assert landed.id != case.thread.id
    copied = (await case.app.conversations.for_thread(landed.id))[0]
    assert copied.id != source.id
    assert copied.permission_mode == "default"
    assert copied.runs[-1].model_name == "opus"
    assert len((await case.app.conversations.get(copied.id)).messages) == 2
    assert case.agent.turns[-1].model == "picked-model"
    assert (await case.app.conversations.get(source.id)).thread_id == case.thread.id


@pytest.mark.parametrize("operation", ["teleport", "summon"])
async def test_actions_require_thread_ownership(
    case: Case, client: httpx.AsyncClient, operation: str
) -> None:
    other = await a_user("other")
    case.app.dependency_overrides[current_user] = lambda: other
    path = f"/api/trunkline/threads/{case.thread.id}"
    assert (await client.get(f"{path}/operations")).status_code == 404
    body = {"destination": {"kind": "channel", "channel": "far"}, "hint": "go"}
    if operation == "summon":
        body.update(agent_id="second", model="test", brief="Take over")
    assert (await client.post(f"{path}/{operation}", json=body)).status_code == 404
    assert not case.far.opened_dms


async def test_validation_precedes_side_effects(
    case: Case, client: httpx.AsyncClient
) -> None:
    path = f"/api/trunkline/threads/{case.thread.id}"
    invalid = await client.post(
        f"{path}/summon",
        json={
            "destination": {"kind": "here"},
            "agent_id": "first",
            "model": "test",
            "brief": "Take over",
            "hint": "go",
        },
    )
    assert invalid.status_code == 409
    invalid = await client.post(
        f"{path}/teleport",
        json={"destination": {"kind": "channel", "channel": "unlinked"}, "hint": "go"},
    )
    assert invalid.status_code == 409
    assert not case.far.opened_dms
    assert not case.receiver.streams


async def test_busy_thread_is_refused(case: Case, client: httpx.AsyncClient) -> None:
    conversation = (await case.app.conversations.for_thread(case.thread.id))[0]
    session = OctomateSession(
        channel_routes={}, current_agent_id="first", conversation_id=conversation.id
    )
    async with case.app.gateway.driving(session):
        response = await client.post(
            f"/api/trunkline/threads/{case.thread.id}/teleport",
            json={"destination": {"kind": "channel", "channel": "far"}, "hint": "go"},
        )
    assert response.status_code == 409
    assert not case.far.opened_dms


async def test_failed_open_reports_stream_error(
    case: Case, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(case.far.ink, "open_dm", AsyncMock(return_value=None))
    response = await client.post(
        f"/api/trunkline/threads/{case.thread.id}/teleport",
        json={"destination": {"kind": "channel", "channel": "far"}, "hint": "go"},
    )
    assert "run_error" in response.text
    assert '"event_kind":"gateway"' not in response.text
    assert not case.agent.turns


async def test_summon_requires_the_requested_thread_to_open(
    case: Case, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = ChannelAddress(
        channel_tentacle_id="far",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
    )
    thread = await case.app.thread_manager.ensure(address)
    await case.app.conversations.ensure(thread.id, agent_tentacle_id="first")
    profile = await case.app.users.profile("far", "alice")
    assert profile is not None
    await case.app.thread_manager.record_outbound(
        thread,
        agent_tentacle_id="first",
        sender=profile,
        segments=[TextSegment(data={"text": "Original private conversation"})],
    )
    monkeypatch.setattr(case.far, "start_sub_thread", AsyncMock(return_value=address))
    response = await client.post(
        f"/api/trunkline/threads/{thread.id}/summon",
        json={
            "destination": {"kind": "thread"},
            "agent_id": "second",
            "model": "test",
            "brief": "Investigate in a new thread",
            "hint": "Open a thread",
        },
    )
    assert '"event_kind":"run_error"' in response.text
    assert '"event_kind":"gateway"' not in response.text
    assert not case.receiver.turns
    assert not case.receiver.streams
    unchanged = await case.app.thread_manager.get(thread.id)
    assert unchanged is not None
    assert unchanged.active_agent_tentacle_id == "first"
    assert not unchanged.handoffs


async def test_trunkline_has_no_dm_or_sub_thread_surface(case: Case) -> None:
    assert not case.channel.surfaces.direct_message
    assert not case.channel.surfaces.sub_thread
    assert await case.channel.open_dm(str(case.owner.id)) is None
    assert not case.channel.accepts_sub_thread(
        ChannelAddress(
            channel_tentacle_id=case.channel.id,
            chat_type="dm",
            chat_id=str(case.owner.id),
            user_id=str(case.owner.id),
        )
    )


async def test_trunkline_directives_require_a_thread(case: Case) -> None:
    with pytest.raises(ValidationError):
        TrunklineDirective(thread_id="", user=case.owner, text="hello")
    directive = TrunklineDirective(thread_id=uuid7().hex, user=case.owner, text="hello")
    event = await case.channel.chromo.sip(directive)
    assert event is not None
    assert event.chat_type == "thread"
    assert event.channel_thread_id == directive.thread_id


async def test_unsupported_fork_harness_is_not_offered(
    case: Case, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeAgent, "fork_session", AgentTentacle.fork_session)
    path = f"/api/trunkline/threads/{case.thread.id}"
    options = (await client.get(f"{path}/operations")).json()
    assert options["teleport"]["destinations"] == []
    assert "forking" in options["teleport"]["reason"]
    refused = await client.post(
        f"{path}/teleport",
        json={"destination": {"kind": "channel", "channel": "far"}, "hint": "go"},
    )
    assert refused.status_code == 409
    assert not case.far.opened_dms


async def test_external_thread_unknown_privacy_does_not_export_history(
    case: Case, client: httpx.AsyncClient
) -> None:
    thread = await case.app.thread_manager.ensure(
        ThreadKey("far", "thread", "room", uuid7().hex)
    )
    await case.app.conversations.ensure(thread.id, agent_tentacle_id="first")
    profile = await case.app.users.profile("far", "alice")
    assert profile is not None
    await case.app.thread_manager.record_outbound(
        thread,
        agent_tentacle_id="first",
        sender=profile,
        segments=[TextSegment(data={"text": "shared"})],
    )
    path = f"/api/trunkline/threads/{thread.id}"
    options = (await client.get(f"{path}/operations")).json()
    assert options["teleport"]["destinations"] == []
    assert any(
        one["target"] == {"kind": "channel", "channel": "trunkline"}
        for one in options["summon"]["destinations"]
    )
    response = await client.post(
        f"{path}/teleport",
        json={
            "destination": {"kind": "channel", "channel": "trunkline"},
            "hint": "export",
        },
    )
    assert response.status_code == 409


async def test_trunkline_offers_a_new_thread_without_a_dm(case: Case) -> None:
    profile = await case.app.users.profile("far", "alice")
    assert profile is not None
    gateway = OctomateSession(
        channel_routes=case.app.gateway.available_routes(
            case.app.channels, case.app.agents
        ),
        current_agent_id="first",
        channels=case.app.channels,
        agents=case.app.agents,
        users=case.app.users,
        user_profile=profile,
        conversation_address=ChannelAddress(
            channel_tentacle_id="far", chat_type="dm", chat_id="alice", user_id="alice"
        ),
    )
    assert all(
        one.handle != "trunkline" for one in (await gateway.destinations())["send"]
    )
    [destination] = (await gateway.destinations())["teleport"]
    assert destination.handle == "trunkline"
    assert destination.address.chat_type == "thread"
    assert destination.address.channel_thread_id is None
    await gateway.teleport(
        destination=ChannelTarget(channel="trunkline"), hint="Work in browser"
    )
    assert gateway.decision is not None


async def test_native_teleport_requires_a_transcript_fork_agent(
    case: Case, client: httpx.AsyncClient
) -> None:
    thread = await case.app.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", str(case.owner.id), uuid7().hex)
    )
    await case.app.conversations.ensure(thread.id, agent_tentacle_id=CODEX_NATIVE_ID)
    await case.app.thread_manager.record_outbound(
        thread,
        agent_tentacle_id=CODEX_NATIVE_ID,
        sender=UserProfile(channel_user_id=str(case.owner.id), user_id=case.owner.id),
        segments=[TextSegment(data={"text": "native"})],
    )
    path = f"/api/trunkline/threads/{thread.id}"
    options = (await client.get(f"{path}/operations")).json()
    assert options["teleport"]["destinations"] == []
    assert "No eligible destinations" in options["teleport"]["reason"]
    assert any(
        one["target"] == {"kind": "channel", "channel": "trunkline"}
        for one in options["summon"]["destinations"]
    )
    response = await client.post(
        f"{path}/teleport",
        json={"destination": {"kind": "channel", "channel": "far"}, "hint": "go"},
    )
    assert response.status_code == 409
    assert not case.far.opened_dms


async def test_thread_capability_requires_own_channel_address(case: Case) -> None:
    channel = case.far
    address = ChannelAddress(
        channel_tentacle_id=channel.id,
        chat_type="dm",
        chat_id="account",
        user_id="account",
    )
    assert channel.accepts_sub_thread(address)
    assert not channel.accepts_sub_thread(
        replace(address, channel_tentacle_id="another-channel")
    )


async def test_discord_dm_threads_are_not_offered(case: Case) -> None:
    discord = case.app.connect(
        DiscordTentacle(
            "discord",
            case.app,
            config=DiscordChannelConfig(
                bot_token=SecretStr("test-only"), agents=["first", "second"]
            ),
        )
    )
    discord.self_profile = UserProfile(channel_user_id="bot", name="Discord")
    profile = await case.app.users.ensure_profile(
        "discord", UserProfile(channel_user_id="discord-user", user_id=case.owner.id)
    )
    session = OctomateSession(
        channel_routes=case.app.gateway.available_routes(
            case.app.channels, case.app.agents
        ),
        current_agent_id="first",
        channels=case.app.channels,
        agents=case.app.agents,
        users=case.app.users,
        user_profile=UserProfile(
            channel_tentacle_id="trunkline",
            channel_user_id=str(case.owner.id),
            user_id=case.owner.id,
        ),
        conversation_address=ChannelAddress(
            channel_tentacle_id="trunkline",
            chat_type="thread",
            chat_id=str(case.owner.id),
            channel_thread_id=uuid7().hex,
            user_id=str(case.owner.id),
        ),
    )
    options = await session.operations
    assert all(
        destination.target.handle != "discord"
        for destination in options.teleport.destinations + options.summon.destinations
    )
    dm = ChannelAddress(
        channel_tentacle_id="discord",
        chat_type="dm",
        chat_id="dm",
        user_id=profile.channel_user_id,
    )
    assert not discord.accepts_sub_thread(dm)
    group = ChannelAddress(
        channel_tentacle_id="discord",
        chat_type="group",
        chat_id="room",
        user_id=profile.channel_user_id,
        shared=True,
    )
    assert discord.accepts_sub_thread(group)
    assert not discord.accepts_sub_thread(
        replace(group, channel_tentacle_id="another-discord")
    )


@pytest.mark.parametrize("operation", ["teleport", "summon"])
async def test_new_trunkline_destination_is_owned_and_independent(
    case: Case, client: httpx.AsyncClient, operation: str
) -> None:
    body = {
        "destination": {"kind": "channel", "channel": "trunkline"},
        "hint": "New workspace",
    }
    if operation == "summon":
        body.update(agent_id="second", model="test", brief="Continue the investigation")
    response = await client.post(
        f"/api/trunkline/threads/{case.thread.id}/{operation}", json=body
    )
    assert "run_error" not in response.text, response.text
    assert '"event_kind":"gateway"' in response.text
    listed = await case.app.thread_manager.list_threads(user_id=case.owner.id)
    [landed] = [thread for thread in listed if thread.id != case.thread.id]
    assert landed.channel_tentacle_id == "trunkline"
    assert landed.chat_type == "thread"
    assert landed.channel_thread_id != case.thread.channel_thread_id
    assert landed.active_agent_tentacle_id == (
        "first" if operation == "teleport" else "second"
    )
    original = await case.app.thread_manager.get(case.thread.id)
    assert original is not None
    assert original.active_agent_tentacle_id == "first"
