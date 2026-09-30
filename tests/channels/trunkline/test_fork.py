"""Forking native Codex history through the authenticated console API."""

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from octomate.auth import current_user
from octomate.config.channels import ChannelConfig, TrunklineChannelConfig
from octomate.database import async_session
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import CODEX_NATIVE_ID, Thread
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
    thread = await app.thread_manager.get(case.source.thread_id)
    assert thread is not None
    await app.thread_manager.record_outbound(
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


async def test_fork_creates_an_owned_driven_thread(
    case: ForkCase, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = case.tentacle.octomate
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 201, response.text
    destination = response.json()
    assert destination["channel_tentacle_id"] == "trunkline"
    assert destination["chat_id"] == str(case.owner_id)
    assert UUID(destination["channel_thread_id"]).version == 7
    assert destination["id"] != str(case.source.thread_id)
    assert destination["handoffs"] == []
    assert destination["active_agent_tentacle_id"] == case.tentacle.id
    detail = await client.get(f"/api/trunkline/threads/{destination['id']}")
    assert detail.status_code == 200
    conversations = await client.get(
        f"/api/trunkline/threads/{destination['id']}/conversations"
    )
    [forked] = conversations.json()
    assert forked["external_id"] == case.fork.return_value
    assert forked["permission_mode"] == "auto_review"
    assert forked["runs"][-1]["model_name"] == "gpt-6-luna"
    assert forked["transcript_file_id"] != str(case.source.transcript_file_id)
    assert (
        await app.files.read(UUID(forked["transcript_file_id"]), owner_id=case.owner_id)
        == case.prefix
    )
    assert (
        await app.conversations.get(case.source.id)
    ).external_id == case.source.external_id

    stored = await app.thread_manager.get(UUID(destination["id"]))
    assert stored is not None
    assert stored.active_agent_tentacle_id == case.tentacle.id
    [notice] = stored.messages
    assert notice.actor_kind == "system"
    assert notice.direction == "inbound"
    assert notice.platform_message_id is None
    assert notice.message_text == (
        f"Forked from conversation {case.source.id}.\n\n"
        "This fork has no server project. The source working directory "
        "and its files were not transferred.\n\n"
        f"Current channel address:\n{stored.key}/{case.owner_id}."
    )
    assert stored.source_cursor_message_id is None
    pending = await app.thread_manager.pending_prompt_messages(
        stored, notice.id, case.tentacle.id
    )
    assert [message.id for message in pending] == [notice.id]

    kick = AsyncMock()
    monkeypatch.setattr(app, "kick", kick)
    sent = await client.post(
        f"/api/trunkline/threads/{destination['channel_thread_id']}/messages",
        json={"text": "continue the work"},
    )
    assert sent.status_code == 200
    kick.assert_awaited_once()
    assert case.fork.await_count == 1
    after = await app.thread_manager.get(stored.id)
    assert after is not None
    assert after.handoffs == []
    pending = await app.thread_manager.pending_prompt_messages(
        after, after.messages[-1].id, case.tentacle.id
    )
    assert [message.id for message in pending] == [notice.id, after.messages[-1].id]
    assert pending[-1].message_text == "continue the work"

    refused = await client.post(
        f"/api/trunkline/threads/{destination['channel_thread_id']}/messages",
        json={"text": "switch agent", "model": "another-agent:"},
    )
    assert refused.status_code == 409
    assert kick.await_count == 1

    other = await a_user("other-reader")
    app.dependency_overrides[current_user] = lambda: other
    assert (
        await client.get(f"/api/trunkline/threads/{destination['id']}")
    ).status_code == 404


@pytest.mark.parametrize(
    "availability", ["available", "missing", "disabled", "unregistered"]
)
async def test_fork_uses_only_an_available_server_project(
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

    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 201, response.text
    destination = response.json()
    available = availability == "available"
    assert destination["project_id"] == (str(project.id) if available else None)
    assert (
        case.fork.call_args.kwargs["cwd"]
        == app.workspaces.open(
            UUID(destination["id"]), project if available else None
        ).path
    )
    stored_source = await app.thread_manager.get(case.source.thread_id)
    assert stored_source is not None
    assert stored_source.project_id == project.id


async def test_fork_requires_source_ownership(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    other = await a_user("other-owner")
    case.tentacle.octomate.dependency_overrides[current_user] = lambda: other
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 404
    case.fork.assert_not_awaited()


async def test_fork_requires_csrf_header(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    client.headers.pop("X-Octomate-Request")
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 403
    case.fork.assert_not_awaited()


async def test_thread_access_does_not_grant_transcript_ownership(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    app = case.tentacle.octomate
    other = await a_user("participant")
    source = await app.thread_manager.get(case.source.thread_id)
    assert source is not None
    await app.thread_manager.record_outbound(
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
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 404
    case.fork.assert_not_awaited()


async def test_fork_requires_a_codex_agent(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    case.tentacle.octomate.tentacles.pop(case.tentacle.id)
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 503
    case.fork.assert_not_awaited()


async def test_failed_fork_does_not_publish_a_destination(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    case.fork.side_effect = ValueError(
        "No completed Codex turn has been fully uploaded"
    )
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 409
    assert "No completed Codex turn" in response.json()["detail"]
    listed = await client.get("/api/trunkline/threads")
    assert [row["id"] for row in listed.json()] == [str(case.source.thread_id)]


async def test_fork_requires_the_thread_id(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    wrong_id = await client.post(f"/api/trunkline/threads/{case.source.id}/fork")
    assert wrong_id.status_code == 404
    old_entry = await client.post(f"/api/trunkline/conversations/{case.source.id}/fork")
    assert old_entry.status_code == 404
    case.fork.assert_not_awaited()


async def test_fork_uses_the_threads_active_conversation(
    case: ForkCase, client: httpx.AsyncClient
) -> None:
    await case.tentacle.octomate.conversations.ensure(
        case.source.thread_id, agent_tentacle_id="another-agent"
    )
    response = await client.post(f"/api/trunkline/threads/{case.source.thread_id}/fork")
    assert response.status_code == 422
    case.fork.assert_not_awaited()


@pytest.mark.parametrize("destination", ["trunkline", "far"])
async def test_native_teleport_imports_completed_history_before_resuming(
    case: ForkCase,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    app = case.tentacle.octomate
    app.connect(CodexTentacle("other-codex", app, config=case.tentacle.config))
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
    assert any(
        one["target"] == {"kind": "channel", "channel": destination}
        for one in options["teleport"]["destinations"]
    )
    response = await client.post(
        f"{path}/teleport",
        json={
            "destination": {"kind": "channel", "channel": destination},
            "hint": "Continue here",
        },
    )
    assert "run_error" not in response.text, response.text
    assert '"event_kind":"gateway"' in response.text
    listed = await app.thread_manager.list_threads(user_id=case.owner_id)
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
    assert [run.model for run in (*runner.turns, *runner.streams)] == ["test"]
    assert (
        await app.conversations.get(case.source.id)
    ).external_id == case.source.external_id
