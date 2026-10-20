"""Codex-owned apps reuse artifact storage, replay and channel delivery."""

from unittest.mock import AsyncMock

import httpx
import pytest
from openai_codex.generated.v2_all import ItemCompletedNotification, ThreadItem
from pydantic import AnyHttpUrl, BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.auth import current_user
from octomate.capabilities.harness.events import ArtifactAvailableEvent
from octomate.config import OctomateConfig
from octomate.config.agents import CodexConfig
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.schemas.artifacts import CapturedMcpAppSource, McpAppArtifact, McpAppView
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.files import Html
from octomate.schemas.messages import ModelResponse
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import MessageBinding
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex import ink as codex_ink
from octomate.types.json import JsonObject
from tests.agent.test_codex_tentacle import (
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


@pytest.mark.parametrize("server", ["apps", "octomate_driven"])
async def test_driven_app_survives_its_session_and_reuses_the_owned_viewer(
    monkeypatch: pytest.MonkeyPatch, in_memory_engine: AsyncEngine, server: str
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
    address = ChannelAddress(
        channel_tentacle_id="im",
        chat_type="thread",
        chat_id="alice",
        channel_thread_id="app-test",
        user_id="alice",
    )
    thread = await host.threads.enter(address)
    await host.threads.record_inbound(
        MessageEvent(
            tentacle_id="im",
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=profile,
            segments=[TextSegment(data={"text": "Show the weather"})],
        )
    )
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
    result: JsonObject = {
        "content": [{"type": "text", "text": "Chart ready"}],
        "structuredContent": {"city": "上海", "temperature": 20},
        "_meta": {"uiOnly": {"revision": 2}},
    }
    html = "<html><body>Chart ✓</body></html>"
    ui = {"csp": {"connectDomains": ["https://api.example"]}}

    def app_turn(thread_id: str) -> None:
        completed = notification(
            "item/completed",
            ItemCompletedNotification(
                thread_id=thread_id,
                turn_id="turn-1",
                completed_at_ms=1,
                item=ThreadItem.model_validate(
                    {
                        "id": "app-call",
                        "type": "mcpToolCall",
                        "server": server,
                        "tool": "show_chart",
                        "arguments": {"city": "上海"},
                        "status": "completed",
                        "result": result,
                        "mcpAppUi": {
                            "resourceUri": "ui://chart/app.html",
                            "preferredModelDisplayMode": "inline",
                        },
                        "appContext": {"connectorId": "weather", "linkId": "account-a"},
                    }
                ),
            ),
        )
        FakeCodex.script = [
            completed,
            completed,
            *text_script("Done", thread_id=thread_id),
        ]

    FakeCodex.on_turn = app_turn
    reads: list[JsonObject | None] = []
    async with tentacle:
        request = tentacle.ink.client._client.request

        async def resource_request[ResponseT: BaseModel](
            method: str, params: JsonObject | None, *, response_model: type[ResponseT]
        ) -> ResponseT:
            if method != "mcpServer/resource/read":
                return await request(method, params, response_model=response_model)
            reads.append(params)
            return response_model.model_validate(
                {
                    "originCallId": "app-call",
                    "contents": [
                        {
                            "uri": "ui://chart/app.html",
                            "mimeType": "text/html;profile=mcp-app",
                            "text": html,
                            "_meta": {"ui": ui},
                        }
                    ],
                }
            )

        monkeypatch.setattr(tentacle.ink.client._client, "request", resource_request)
        async with tentacle.run_stream_events(
            "Show the weather", conversation_address=address, thread_id=thread.id
        ) as stream:
            events = [event async for event in stream]

    available = [event for event in events if isinstance(event, ArtifactAvailableEvent)]
    if server == "octomate_driven":
        assert available == []
        assert reads == []
        return
    [event] = available
    assert FakeCodex.closed == 1
    assert reads == [
        {
            "threadId": "thread-new",
            "server": "apps",
            "uri": "ui://chart/app.html",
            "originCallId": "app-call",
            "target": {"connectorId": "weather", "linkId": "account-a"},
        }
    ]
    async with async_session() as session:
        artifact = await session.get(McpAppArtifact, event.artifact_id)
        binding = await session.one_or_none(
            MessageBinding,
            expressions=[MessageBinding["thread_message_id"] == event.message_id],
        )
        assert binding is not None
        response = await session.get(ModelResponse, binding.model_message_id)
        assert response is not None
        assert response.provider_response_id == "app-call"
    assert artifact is not None
    assert isinstance(artifact.source, CapturedMcpAppSource)
    stored = await host.files.get(artifact.source.file_id, owner_id=owner.id)
    assert isinstance(stored, Html)
    assert await host.files.read(stored.id, owner_id=owner.id) == html.encode()
    assert artifact.invocation.tool is None
    assert artifact.invocation.arguments == {"city": "上海"}
    assert artifact.invocation.result.structured_content == result["structuredContent"]
    assert artifact.invocation.result.meta == result["_meta"]
    assert "<html>" not in artifact.model_dump_json()
    await channel.feelers.artifacts.present(address, event)
    assert channel.sent[-1][2][0]["text"] == (
        f"show_chart\napps · Interactive artifact\nhttps://console.example/artifacts/{artifact.id}"
    )
    monkeypatch.setattr(
        host.mcp,
        "app_resource",
        AsyncMock(side_effect=AssertionError("Captured apps must not reconnect")),
    )
    host.dependency_overrides[current_user] = lambda: owner
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host), base_url="https://console.example"
    ) as client:
        for url in (
            f"/api/artifacts/{artifact.id}",
            f"/api/trunkline/threads/{thread.id}/artifacts/{event.message_id}",
        ):
            response = await client.get(url)
            assert response.status_code == 200, (url, response.text)
            view = McpAppView.model_validate(response.json())
            assert view.resource.html == html
            assert view.resource.ui.csp is not None
            assert view.resource.ui.csp.connect_domains == ["https://api.example"]
            assert response.headers["cache-control"] == "no-store"
        other = await a_user("bob")
        host.dependency_overrides[current_user] = lambda: other
        assert (await client.get(f"/api/artifacts/{artifact.id}")).status_code == 404
        host.dependency_overrides[current_user] = lambda: owner
        await host.files.storage.delete(stored.key)
        assert (await client.get(f"/api/artifacts/{artifact.id}")).status_code == 404
