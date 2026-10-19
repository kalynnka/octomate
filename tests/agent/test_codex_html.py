"""HTML capture uses the run's workspace and owner, then survives source deletion."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from openai_codex.generated.v2_all import AgentMessageDeltaNotification
from pydantic import AnyHttpUrl
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.auth import current_user
from octomate.capabilities.harness.events import ArtifactAvailableEvent
from octomate.config import OctomateConfig
from octomate.config.agents import CodexConfig
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.schemas.artifacts import Artifact, HtmlArtifact, HtmlView
from octomate.schemas.files import Html
from octomate.schemas.thread import ThreadArtifact
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex import ink as codex_ink
from octomate.tentacles.codex.adapter import CodexRunAccumulator
from octomate.tentacles.codex.html import HtmlReference, visualization_text
from tests.agent.test_codex_tentacle import (
    KEY,
    FakeCodex,
    notification,
    reset_fake_codex,
    text_script,
)
from tests.agent.test_codex_tentacle import (
    native_thread_handles as native_thread_handles,
)
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user, auth_config


@pytest.mark.parametrize(
    "text",
    ["An unfinished visual", "```html\n<p>Hello</p>\n```", "[chart](chart.html)"],
)
def test_ordinary_text_and_incomplete_markers_remain_visible(text: str) -> None:
    accumulator = CodexRunAccumulator()
    events = [
        event for item in text_script(text) for event in accumulator.consume(item)
    ]
    visible = "".join(
        event.delta.content_delta
        for event in events
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta)
    )
    assert visible == text
    assert accumulator.result_text == text


@pytest.mark.parametrize("split", range(1, 16))
def test_references_are_hidden_across_every_marker_boundary(split: int) -> None:
    text = 'Before visualize{"path":"/workspace/chart.html"} after'
    pending = ""
    output = ""
    for index in range(0, len(text), split):
        visible, pending = visualization_text(pending + text[index : index + split])
        output += visible
    assert output == "Before  after"
    assert pending == ""
    assert (
        visualization_text("A citesource citation")[0] == "A citesource citation"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "outside",
        "traversal",
        "file_link",
        "directory_link",
        "fifo",
        "large",
        "encoding",
        "blank",
        "suffix",
        "missing",
    ],
)
def test_capture_refuses_invalid_files(tmp_path: Path, invalid: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("<p>private</p>")
    source = workspace / "chart.html"
    if invalid == "outside":
        source = outside
    elif invalid == "traversal":
        source = workspace / ".." / outside.name
    elif invalid == "file_link":
        source.symlink_to(outside)
    elif invalid == "directory_link":
        (workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
        source = workspace / "linked" / outside.name
    elif invalid == "fifo":
        os.mkfifo(source)
    elif invalid == "large":
        source.write_bytes(b"x" * 1_000_001)
    elif invalid == "encoding":
        source.write_bytes(b"\xff")
    elif invalid == "blank":
        source.write_text(" \n\t")
    elif invalid == "suffix":
        source = workspace / "secret.txt"
        source.write_text("private")
    with pytest.raises((ValueError, OSError)):
        HtmlReference(path=source).read(workspace)


@pytest.mark.parametrize("chunk_size", [1, 17, 1000])
async def test_driven_html_reaches_the_owned_viewer_and_channel_card(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
    chunk_size: int,
) -> None:
    monkeypatch.setattr(codex_ink, "SharedCodex", FakeCodex)
    reset_fake_codex([])
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.config.artifacts.viewer_origin = AnyHttpUrl("https://console.example")
    host.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    owner = await a_user("alice")
    profile = await host.users.ensure_profile(
        "im", UserProfile(channel_user_id="alice", user_id=owner.id)
    )
    channel = FakeChannelTentacle(octomate=host)
    thread = await host.threads.enter(KEY)
    conversation = await host.conversations.ensure(thread.id, agent_tentacle_id="codex")
    host.gateway.register(
        OctomateSession(
            channel_routes={},
            channels={"im": channel},
            current_agent_id="codex",
            conversation_id=conversation.id,
            user_profile=profile,
        )
    )
    tentacle = CodexTentacle(
        "codex",
        host,
        config=CodexConfig(permission_mode="auto_review"),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        auth=host.auth,
        gateway_manager=host.gateway,
    )
    workspace = host.workspaces.open(thread.id, None).path
    source = workspace / "chart.html"
    html = "<button onclick=\"this.textContent = 'Updated'\">Update</button>"

    def write_visual(_thread_id: str) -> None:
        source.write_text(html)
        reference = (
            "visualize"
            + json.dumps({"path": str(source), "title": "Chart", "mode": "wide"})
            + ""
        )
        answer = f"Here is the chart.\n{reference}\n{reference}"
        script = text_script(answer, thread_id=_thread_id)
        FakeCodex.script = [
            notification(
                "item/agentMessage/delta",
                AgentMessageDeltaNotification(
                    delta=answer[index : index + chunk_size],
                    item_id="msg-1",
                    thread_id=_thread_id,
                    turn_id="turn-1",
                ),
            )
            for index in range(0, len(answer), chunk_size)
        ] + script[1:]

    FakeCodex.on_turn = write_visual
    async with (
        tentacle,
        tentacle.run_stream_events(
            "Draw a chart", conversation_address=KEY, thread_id=thread.id
        ) as stream,
    ):
        events = [event async for event in stream]
    [available] = [
        event for event in events if isinstance(event, ArtifactAvailableEvent)
    ]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "Here is the chart.\n\n"
    deltas = "".join(
        event.delta.content_delta
        for event in events
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta)
    )
    assert deltas == "Here is the chart.\n\n"
    assert FakeCodex.thread_calls[0].developer_instructions is None
    assert not source.exists()  # The run has already released its temporary workspace.
    async with async_session() as session:
        artifact = await session.one_or_none(
            Artifact, expressions=[Artifact["id"] == available.artifact_id]
        )
        entry = await session.get(ThreadArtifact, available.message_id)
    assert isinstance(artifact, HtmlArtifact)
    assert "html" not in artifact.model_dump()
    stored = await host.files.get(artifact.file_id, owner_id=owner.id)
    assert isinstance(stored, Html)
    assert stored.name == source.name
    assert await host.files.read(stored.id, owner_id=owner.id) == html.encode("utf-8")
    with pytest.raises(FileNotFoundError):
        await host.files.read(stored.id)
    assert artifact.owner_id == owner.id
    assert entry is not None
    assert "html" not in entry.model_dump(mode="json")
    await channel.feelers.artifacts.present(KEY, available)
    assert (
        channel.sent[-1][2][0]["text"]
        == f"Chart\nGenerated HTML · Interactive artifact\nhttps://console.example/artifacts/{artifact.id}"
    )
    monkeypatch.setattr(
        host.mcp,
        "app_resource",
        AsyncMock(side_effect=AssertionError("HTML must not contact MCP")),
    )
    host.dependency_overrides[current_user] = lambda: owner
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host), base_url="https://console.example"
    ) as client:
        response = await client.get(f"/api/artifacts/{artifact.id}")
        assert response.status_code == 200
        view = HtmlView.model_validate(response.json())
        assert view.html == html
        assert view.artifact.file_id == stored.id
        assert "resource" not in response.json()
        assert response.headers["cache-control"] == "no-store"
        assert "html" in str(view.sandbox_url)
        other = await a_user("bob")
        host.dependency_overrides[current_user] = lambda: other
        assert (await client.get(f"/api/artifacts/{artifact.id}")).status_code == 404
        host.dependency_overrides[current_user] = lambda: owner
        await host.files.storage.delete(stored.key)
        missing = await client.get(f"/api/artifacts/{artifact.id}")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Artifact file unavailable"
