"""Teleporting native Codex history through the authenticated console API: the
import into a thread a driven agent carries on in."""

from collections.abc import AsyncGenerator
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import UploadFile

from octomate.auth import current_user
from octomate.config.channels import ChannelConfig, TrunklineChannelConfig
from octomate.database import async_session
from octomate.schemas.awakes import NativeGatewaySignal
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import CODEX_NATIVE_ID, Thread
from octomate.schemas.triage import TeleportDecision
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.trunkline import TrunklineTentacle
from tests.agent.test_codex_transcript_fork import ForkCase
from tests.agent.test_codex_transcript_fork import case as case
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project
from tests.support.users import a_user


@pytest.fixture
async def client(case: ForkCase) -> AsyncGenerator[httpx.AsyncClient]:
    app = case.tentacle.octomate
    async with async_session() as session:
        owner = await session.get(User, case.owner_id)
    assert owner is not None
    app.dependency_overrides[current_user] = lambda: owner
    app.connect(case.tentacle)
    channel = app.connect(
        TrunklineTentacle(
            "trunkline", app, config=TrunklineChannelConfig(agents=[case.tentacle.id])
        )
    )
    await channel.probe()
    thread = await app.threads.get(case.source.thread_id)
    assert thread is not None
    await app.threads.record_outbound(
        thread,
        agent_tentacle_id=CODEX_NATIVE_ID,
        segments=[TextSegment(data={"text": "Native source history"})],
        sender=UserProfile(
            channel_user_id=owner.username, user_id=owner.id, name=owner.name
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        yield client


async def teleport_to_trunkline(case: ForkCase, client: httpx.AsyncClient) -> Thread:
    """The console's teleport of the native thread into a new Trunkline thread, and
    the thread it landed in."""
    app = case.tentacle.octomate
    path = f"/api/trunkline/threads/{case.source.thread_id}"
    options = (await client.get(f"{path}/operations")).json()
    [address] = [
        one
        for one in options["teleport"]["destinations"]
        if one["channel_tentacle_id"] == "trunkline"
    ]
    response = await client.post(
        f"{path}/teleport", json={"destination": address, "hint": "Continue here"}
    )
    assert "run_error" not in response.text, response.text
    listed = await app.threads.list_threads(user_id=case.owner_id)
    [landed] = [one for one in listed if one.channel_tentacle_id == "trunkline"]
    stored = await app.threads.get(landed.id)
    assert stored is not None
    return stored


async def test_a_native_session_lands_in_an_owned_thread_its_agent_carries_on(
    case: ForkCase, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = case.tentacle.octomate
    stored = await teleport_to_trunkline(case, client)

    assert stored.chat_id == str(case.owner_id)
    assert stored.handoffs == []
    assert stored.active_agent_tentacle_id == case.tentacle.id
    notice = stored.messages[-1]
    assert notice.actor_kind == "system"
    assert notice.direction == "inbound"
    assert notice.platform_message_id is None
    assert notice.message_text == (
        f"Forked from conversation {case.source.id}.\n\n"
        "This fork has no server project. The source working directory "
        "and its files were not transferred.\n\n"
        f"Current channel address:\n{stored.key}/{case.owner_id}."
    )
    pending = await app.threads.pending_prompt_messages(
        stored, notice.id, case.tentacle.id
    )
    assert [message.id for message in pending] == [notice.id]

    kick = AsyncMock()
    monkeypatch.setattr(app, "kick", kick)
    sent = await client.post(
        f"/api/trunkline/threads/{stored.channel_thread_id}/messages",
        json={"text": "continue the work"},
    )
    assert sent.status_code == 200
    kick.assert_awaited_once()
    after = await app.threads.get(stored.id)
    assert after is not None
    assert after.handoffs == []
    pending = await app.threads.pending_prompt_messages(
        after, after.messages[-1].id, case.tentacle.id
    )
    assert [message.id for message in pending] == [notice.id, after.messages[-1].id]
    assert pending[-1].message_text == "continue the work"

    refused = await client.post(
        f"/api/trunkline/threads/{stored.channel_thread_id}/messages",
        json={"text": "switch agent", "model": "another-agent:"},
    )
    assert refused.status_code == 409
    assert kick.await_count == 1

    other = await a_user("other-reader")
    app.dependency_overrides[current_user] = lambda: other
    assert (await client.get(f"/api/trunkline/threads/{stored.id}")).status_code == 404


@pytest.mark.parametrize(
    "availability", ["available", "missing", "disabled", "unregistered"]
)
async def test_a_native_session_lands_on_its_project_only_where_it_is_served(
    case: ForkCase,
    client: httpx.AsyncClient,
    tmp_path: Path,
    availability: str,
) -> None:
    app = case.tentacle.octomate
    root = tmp_path / "project"
    if availability != "missing":
        root.mkdir()
    project = a_project(root, name="native-project", enabled=availability != "disabled")
    async with async_session() as session:
        session.add(project)
        await session.flush()
        source = await session.get(Thread, case.source.thread_id)
        assert source is not None
        source.project_id = project.id
        await session.commit()
    if availability != "unregistered":
        app.projects.index([project])

    stored = await teleport_to_trunkline(case, client)

    available = availability == "available"
    assert stored.project_id == (project.id if available else None)
    assert (
        case.fork.call_args.kwargs["cwd"]
        == app.workspaces.open(stored.id, project if available else None).path
    )
    stored_source = await app.threads.get(case.source.thread_id)
    assert stored_source is not None
    assert stored_source.project_id == project.id


async def test_thread_access_does_not_grant_transcript_ownership(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    app = case.tentacle.octomate
    other = await a_user("participant")
    source = await app.threads.get(case.source.thread_id)
    assert source is not None
    await app.threads.record_outbound(
        source,
        agent_tentacle_id=CODEX_NATIVE_ID,
        segments=[TextSegment(data={"text": "Shared thread access"})],
        sender=UserProfile(
            channel_user_id=other.username, user_id=other.id, name=other.name
        ),
    )
    app.dependency_overrides[current_user] = lambda: other
    visible = await client.get(f"/api/trunkline/threads/{case.source.thread_id}")
    assert visible.status_code == 200
    response = await client.post(
        f"/api/trunkline/threads/{case.source.thread_id}/teleport",
        json={
            "destination": {
                "channel_tentacle_id": "trunkline",
                "chat_type": "thread",
                "chat_id": str(other.id),
                "user_id": str(other.id),
            },
            "hint": "Continue here",
        },
    )
    # Refused before anything moves: the transcript is its owner's alone.
    assert '"event_kind":"run_error"' in response.text
    case.fork.assert_not_awaited()
    listed = await app.threads.list_threads(user_id=other.id)
    assert [thread.id for thread in listed] == [case.source.thread_id]


@pytest.mark.parametrize("asked_by", ["console", "session"])
@pytest.mark.parametrize("destination", ["trunkline", "far"])
async def test_native_teleport_imports_completed_history_before_resuming(
    case: ForkCase,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    asked_by: str,
) -> None:
    """The console's teleport of a native thread and the native session's own
    enter at the same thread and carry the same history."""
    app = case.tentacle.octomate
    app.connect(
        CodexTentacle(
            "other-codex",
            app,
            config=case.tentacle.config,
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            auth=app.auth,
            gateway_manager=app.gateway,
        )
    )
    if destination == "far":
        far = app.connect(
            FakeChannelTentacle(
                "far", app, config=ChannelConfig(type="fake", agents=[case.tentacle.id])
            )
        )
        await far.probe()
        await app.users.ensure_profile(
            "far", UserProfile(channel_user_id="alice", user_id=case.owner_id)
        )
    case.tentacle.models = {"gpt-6-luna": "test"}
    runner = FakeAgent(id=case.tentacle.id, allow_reception_run=True)
    monkeypatch.setattr(case.tentacle, "run", runner.run)
    monkeypatch.setattr(case.tentacle, "run_stream_events", runner.run_stream_events)
    path = f"/api/trunkline/threads/{case.source.thread_id}"
    options = (await client.get(f"{path}/operations")).json()
    assert options["teleport"]["reason"] is None
    [address] = [
        one
        for one in options["teleport"]["destinations"]
        if one["channel_tentacle_id"] == destination
    ]
    if asked_by == "console":
        response = await client.post(
            f"{path}/teleport",
            json={
                "destination": address,
                "hint": "Continue here",
            },
        )
        assert "run_error" not in response.text, response.text
        assert '"event_kind":"gateway"' in response.text
    else:
        async with async_session() as session:
            user = await session.get(User, case.owner_id)
        assert user is not None
        owner = await app.users.native_profile(CODEX_NATIVE_ID, user.username)
        source = await app.threads.get(case.source.thread_id)
        assert owner is not None
        assert source is not None
        await app.kick(
            NativeGatewaySignal(
                decision=TeleportDecision(
                    agent_id=case.tentacle.id,
                    hint="Continue here",
                    destination=ChannelAddress(**address),
                ),
                agent_id=CODEX_NATIVE_ID,
                user_profile=owner,
                source=source.key.address(owner.channel_user_id),
            )
        )
    listed = await app.threads.list_threads(user_id=case.owner_id)
    [landed] = [
        thread for thread in listed if thread.channel_tentacle_id == destination
    ]
    [copied] = await app.conversations.for_thread(landed.id)
    copied = await app.conversations.get(copied.id)
    assert copied.agent_tentacle_id == case.tentacle.id
    assert copied.permission_mode == "auto_review"
    assert copied.runs[-1].model_name == "gpt-6-luna"
    assert copied.external_id == case.fork.return_value
    assert copied.transcript_file_id is not None
    assert (
        await app.files.read(copied.transcript_file_id, owner_id=case.owner_id)
        == case.prefix
    )
    assert not landed.handoffs
    # Nothing runs there until someone writes: neither the console nor the session
    # asked to carry on.
    assert [*runner.turns, *runner.streams] == []
    assert (
        await app.conversations.get(case.source.id)
    ).external_id == case.source.external_id


async def test_a_native_session_that_asks_to_carry_on_runs_where_it_lands(
    case: ForkCase, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = case.tentacle.octomate
    case.tentacle.models = {"gpt-6-luna": "test"}
    runner = FakeAgent(id=case.tentacle.id, allow_reception_run=True)
    monkeypatch.setattr(case.tentacle, "run", runner.run)
    monkeypatch.setattr(case.tentacle, "run_stream_events", runner.run_stream_events)
    options = (
        await client.get(f"/api/trunkline/threads/{case.source.thread_id}/operations")
    ).json()
    [address] = [
        one
        for one in options["teleport"]["destinations"]
        if one["channel_tentacle_id"] == "trunkline"
    ]
    async with async_session() as session:
        user = await session.get(User, case.owner_id)
    assert user is not None
    owner = await app.users.native_profile(CODEX_NATIVE_ID, user.username)
    source = await app.threads.get(case.source.thread_id)
    assert owner is not None
    assert source is not None

    await app.kick(
        NativeGatewaySignal(
            decision=TeleportDecision(
                agent_id=case.tentacle.id,
                hint="Continue here",
                destination=ChannelAddress(**address),
                resume=True,
            ),
            agent_id=CODEX_NATIVE_ID,
            user_profile=owner,
            source=source.key.address(owner.channel_user_id),
        )
    )

    [run] = [*runner.turns, *runner.streams]
    assert run.model == "test"
    assert "Continuing the conversation here." in str(run.prompt)


async def test_a_native_session_with_a_prompt_answers_it_where_it_lands(
    case: ForkCase, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt is the user's next message there, so the turn reads it with the
    import notice still waiting, as a message typed there would."""
    case.tentacle.models = {"gpt-6-luna": "test"}
    runner = FakeAgent(id=case.tentacle.id, allow_reception_run=True)
    monkeypatch.setattr(case.tentacle, "run", runner.run)
    monkeypatch.setattr(case.tentacle, "run_stream_events", runner.run_stream_events)
    path = f"/api/trunkline/threads/{case.source.thread_id}"
    options = (await client.get(f"{path}/operations")).json()
    [address] = [
        one
        for one in options["teleport"]["destinations"]
        if one["channel_tentacle_id"] == "trunkline"
    ]

    response = await client.post(
        f"{path}/teleport",
        json={"destination": address, "hint": "Continue here", "prompt": "Ship it"},
    )

    assert "run_error" not in response.text, response.text
    [run] = [*runner.turns, *runner.streams]
    assert run.model == "test"
    assert "Ship it" in str(run.prompt)
    assert f"Forked from conversation {case.source.id}." in str(run.prompt)


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("upload", "has not been uploaded"),
        ("incomplete", "No completed Codex turn"),
        ("permissions", "no supported permission preset"),
        ("boundary", "line boundary"),
        ("identity", "source session"),
        ("ancestor", "ancestor files"),
        ("model", "'gpt-6-luna', which 'codex' does not offer"),
    ],
)
async def test_native_teleport_validates_history_before_opening_destination(
    case: ForkCase,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
    message: str,
) -> None:
    app = case.tentacle.octomate
    case.tentacle.models = (
        {"gpt-6-sol": "test"} if invalid == "model" else {"gpt-6-luna": "test"}
    )
    far = app.connect(
        FakeChannelTentacle(
            "far", app, config=ChannelConfig(type="fake", agents=[case.tentacle.id])
        )
    )
    await far.probe()
    await app.users.ensure_profile(
        "far", UserProfile(channel_user_id="alice", user_id=case.owner_id)
    )
    start = AsyncMock(wraps=far.start_thread)
    monkeypatch.setattr(far, "start_thread", start)
    data = case.prefix
    if invalid == "identity":
        data = data.replace(str(case.source.external_id).encode(), b"wrong-session")
    elif invalid == "ancestor":
        data = data.replace(b'"payload": {', b'"payload": {"history_base": {},', 1)
    async with async_session() as session:
        if invalid in {"upload", "identity", "ancestor"}:
            source = await session.get(Conversation, case.source.id)
            assert source is not None
            source.transcript_file_id = None
        run = await session.one(
            ExternalAgentRun,
            expressions=[ExternalAgentRun["end_offset"] == len(case.prefix)],
        )
        if invalid == "incomplete":
            run.end_offset = None
        elif invalid == "permissions":
            run.permission_mode = None
        elif invalid == "boundary":
            run.end_offset = len(case.prefix) - 1
        elif invalid in {"identity", "ancestor"}:
            run.end_offset = len(data)
        await session.commit()
        thread_count = await session.count(Thread)
    if invalid in {"identity", "ancestor"}:
        await app.conversations.store_transcript(
            UploadFile(BytesIO(data), filename="rollout.jsonl"),
            0,
            conversation=case.source,
            files=app.files,
            owner_id=case.owner_id,
        )
    response = await client.post(
        f"/api/trunkline/threads/{case.source.thread_id}/teleport",
        json={
            "destination": {
                "channel_tentacle_id": "far",
                "chat_type": "dm",
                "chat_id": "",
                "user_id": "alice",
            },
            "hint": "Continue here",
        },
    )
    assert '"event_kind":"run_error"' in response.text
    assert message in response.text
    start.assert_not_awaited()
    case.fork.assert_not_awaited()
    assert not far.sent
    assert not case.home.exists()
    async with async_session() as session:
        assert await session.count(Thread) == thread_count
