"""Upstream calls are captured before publication, independently of wrappers."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from cachetools import LRUCache
from fastmcp import Client, Context, FastMCP
from fastmcp.apps.config import UI_EXTENSION_ID, UI_MIME_TYPE
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import AnyHttpUrl, JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.auth import current_user
from octomate.capabilities.harness.events import (
    ArtifactAvailableEvent,
    WireEvent,
    wire_event_adapter,
)
from octomate.config.channels import TrunklineChannelConfig
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpClientKey, McpUnavailable
from octomate.mcp.server import CALL_MCP_TOOL, tentacles_mcp
from octomate.schemas.artifacts import McpAppArtifact
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.mcp import Mcp, McpInstallRequest
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import Thread, ThreadArtifact
from octomate.schemas.user import User
from octomate.tentacles.trunkline import TrunklineTentacle
from tests.managers.test_mcp import connected
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import fixed_session
from tests.support.users import a_user


@dataclass
class AppTurn:
    host: Octomate
    user: User
    scope: OctomateSession
    channel: TrunklineTentacle
    thread: Thread
    installation: Mcp
    server: FastMCP
    upstream: FastMCP
    calls: list[str]
    ui_support: list[bool]


@pytest.fixture
async def app_turn(in_memory_engine: AsyncEngine) -> AppTurn:
    host = Octomate()
    user = await a_user(profiles={"console": "alice"})
    profile = await host.users.profile("console", "alice")
    assert profile is not None
    channel = TrunklineTentacle(
        "console",
        host,
        config=TrunklineChannelConfig(agents=["inkling"]),
    )
    host.connect(channel)
    await channel.probe()
    opening = await host.threads.record_inbound(
        MessageEvent(
            tentacle_id=channel.id,
            chat_type="thread",
            chat_id="alice",
            channel_thread_id="diagram",
            user_id="alice",
            message_id="opening",
            sender=profile,
            segments=[TextSegment(data={"text": "Draw a diagram"})],
        )
    )
    thread = await host.threads.get(opening.thread_id)
    assert thread is not None
    scope = OctomateSession(
        channel_routes={},
        current_agent_id="inkling",
        channels={channel.id: channel},
        user_profile=profile,
        thread_id=thread.id,
        conversation_address=thread.key.address("alice"),
        threads=host.threads,
        nerve=host.nerve,
    )
    installation = await host.mcp.install(
        user.id,
        McpInstallRequest(
            name="Diagrams",
            namespace="diagrams",
            url=AnyHttpUrl("https://mcp.example/mcp"),
        ),
    )
    calls: list[str] = []
    ui_support: list[bool] = []
    upstream = FastMCP("diagrams")

    def draw(xml: str, ctx: Context, fail: bool = False) -> ToolResult:
        calls.append(xml)
        ui_support.append(ctx.client_supports_extension(UI_EXTENSION_ID))
        if ctx.client_supports_extension(UI_EXTENSION_ID):
            assert ctx.session.client_capabilities is not None
            assert ctx.session.client_capabilities.extensions is not None
            assert ctx.session.client_capabilities.extensions[UI_EXTENSION_ID] == {
                "mimeTypes": [UI_MIME_TYPE]
            }
        return ToolResult(
            content=[
                TextContent(type="text", text=xml),
                ImageContent(type="image", data="aW1hZ2U=", mime_type="image/png"),
            ],
            structured_content={"xml": xml, "ready": not fail},
            is_error=fail,
            meta={"uiOnly": {"state": "preserved"}},
        )

    declarations: dict[str, dict[str, JsonValue]] = {
        "draw": {
            "ui": {
                "resourceUri": "ui://diagrams/app.html",
                "visibility": ["model", "app"],
                "vendorHint": {"layout": "wide"},
            },
            "vendor/state": {"enabled": True, "values": [1, None, "diagram"]},
        },
        "legacy": {
            "ui/resourceUri": "ui://diagrams/legacy.html",
            "vendor/state": {"version": 1},
        },
        "ordinary": {"vendor/state": {"version": 1}},
        "invalid": {"ui": {"resourceUri": "https://unrelated.example/app.html"}},
    }
    for name, meta in declarations.items():
        upstream.tool(name=name, meta=meta)(draw)
    server = tentacles_mcp(fixed_session(scope), manager=host.mcp)
    return AppTurn(
        host,
        user,
        scope,
        channel,
        thread,
        installation,
        server,
        upstream,
        calls,
        ui_support,
    )


@pytest.mark.parametrize("name", ["draw", "legacy"])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("discovered", [False, True])
async def test_app_call_is_durable_before_publication(
    app_turn: AppTurn,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    fail: bool,
    discovered: bool,
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream) as requests:
        if discovered:
            await turn.host.mcp.catalog(turn.scope, turn.installation.namespace)
        publish = OctomateSession.publish
        committed: list[McpAppArtifact] = []

        async def after_commit(
            scope: OctomateSession,
            address: ChannelAddress,
            event: WireEvent,
            *,
            private: bool = False,
        ) -> None:
            assert isinstance(event, ArtifactAvailableEvent)
            async with async_session() as session:
                entry = await session.get(ThreadArtifact, event.message_id)
                artifact = await session.get(McpAppArtifact, event.artifact_id)
            assert entry is not None
            assert artifact is not None
            assert entry.artifact_id == artifact.id
            assert artifact.owner_id == turn.user.id
            committed.append(artifact)
            await publish(scope, address, event, private=private)

        monkeypatch.setattr(OctomateSession, "publish", after_commit)
        xml = "<diagram>✓</diagram>" * 100
        with turn.host.nerve.subscribe(turn.thread.key) as live:
            result = await turn.server.call_tool(
                CALL_MCP_TOOL,
                {
                    "namespace": turn.installation.namespace,
                    "name": name,
                    "arguments": {"xml": xml, "fail": fail},
                },
            )
            event = (await live.receive()).event
        assert isinstance(event, ArtifactAvailableEvent)
        assert len(committed) == 1
        saved = committed[0].invocation
        assert saved.installation_id == turn.installation.id
        assert saved.arguments == {"xml": xml, "fail": fail}
        assert saved.tool.name == name
        declared = await turn.upstream.get_tool(name)
        assert declared is not None
        assert saved.tool == declared.to_mcp_tool()
        assert (
            str(saved.resource_uri)
            == f"ui://diagrams/{'app' if name == 'draw' else 'legacy'}.html"
        )
        assert saved.result.content == result.content
        assert saved.result.structured_content == result.structured_content
        assert saved.result.is_error is fail
        assert result.meta is not None
        assert result.meta["uiOnly"] == {"state": "preserved"}
        assert saved.result.meta == result.meta
        public = wire_event_adapter.dump_json(event)
        assert b"uiOnly" not in public
        assert b"arguments" not in public
        assert turn.calls == [xml]
        assert turn.ui_support == [True]
        assert requests.methods.count("tools/list") == 1
        assert requests.methods.count("tools/call") == 1


async def test_two_calls_inside_a_wrapper_remain_two_invocations(
    app_turn: AppTurn,
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream) as requests:
        wrapper = FastMCP("code-wrapper")

        @wrapper.tool(name="functions_exec")
        async def execute() -> str:
            for xml in ("<first />", "<second />"):
                await turn.server.call_tool(
                    CALL_MCP_TOOL,
                    {
                        "namespace": turn.installation.namespace,
                        "name": "draw",
                        "arguments": {"xml": xml},
                    },
                )
            return "wrapper output has no app metadata"

        with turn.host.nerve.subscribe(turn.thread.key) as live:
            async with Client(wrapper) as client:
                await client.call_tool("functions_exec", {})
            events = [(await live.receive()).event, (await live.receive()).event]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(live.receive(), 0.01)
        assert all(isinstance(event, ArtifactAvailableEvent) for event in events)
        page = await turn.host.threads.messages_page(turn.thread.id, limit=10)
        entries = [row for row in page.items if isinstance(row, ThreadArtifact)]
        assert len(entries) == 2
        assert entries[0].id != entries[1].id
        assert entries[0].artifact_id != entries[1].artifact_id
        async with async_session() as session:
            artifacts = [
                await session.get(McpAppArtifact, row.artifact_id) for row in entries
            ]
        assert [
            app.invocation.arguments["xml"] for app in artifacts if app is not None
        ] == [
            "<second />",
            "<first />",
        ]
        assert turn.calls == ["<first />", "<second />"]
        assert requests.methods.count("tools/list") == 1
        assert requests.methods.count("tools/call") == 2


@pytest.mark.parametrize("refresh", ["discovery", "reconnect", "capacity"])
async def test_capture_uses_refreshed_tool_declaration(
    app_turn: AppTurn, refresh: str
) -> None:
    turn = app_turn
    manager = turn.host.mcp
    if refresh == "capacity":
        manager.tool_catalogs = LRUCache(maxsize=1)
    async with connected(manager, turn.upstream) as requests:
        catalog = await manager.catalog(turn.scope, turn.installation.namespace)
        tool = await turn.upstream.get_tool("draw")
        assert tool is not None
        assert catalog.tools_by_name["draw"] == tool.to_mcp_tool()
        updated = tool.model_copy(
            update={"meta": {"ui": {"resourceUri": "ui://diagrams/updated.html"}}}
        )
        turn.upstream.add_tool(updated)
        if refresh == "discovery":
            await manager.catalog(turn.scope, turn.installation.namespace, refresh=True)
        elif refresh == "reconnect":
            async with manager.lock(turn.installation.id):
                await manager.evict(McpClientKey(turn.user.id, turn.installation.id))
        else:
            other = await manager.install(
                turn.user.id,
                McpInstallRequest(
                    name="Other diagrams",
                    namespace="other-diagrams",
                    url=AnyHttpUrl("https://mcp.example/mcp"),
                ),
            )
            await manager.catalog(turn.scope, other.namespace)
            assert McpClientKey(turn.user.id, turn.installation.id) in manager.clients
        with turn.host.nerve.subscribe(turn.thread.key) as live:
            await manager.call_tool(
                turn.scope, turn.installation.namespace, "draw", {"xml": "<updated />"}
            )
            event = (await live.receive()).event
        assert isinstance(event, ArtifactAvailableEvent)
        async with async_session() as session:
            artifact = await session.get(McpAppArtifact, event.artifact_id)
        assert artifact is not None
        assert str(artifact.invocation.resource_uri) == "ui://diagrams/updated.html"
        assert artifact.invocation.tool == updated.to_mcp_tool()
        assert turn.calls == ["<updated />"]
        assert requests.methods.count("tools/list") == (
            3 if refresh == "capacity" else 2
        )
        assert requests.methods.count("tools/call") == 1


@pytest.mark.parametrize(
    "case",
    [
        "ordinary",
        "threadless",
        "native",
        "unconfigured_sandbox",
        "configured_sandbox",
        "other_channel",
    ],
)
async def test_capabilities_and_results_are_independent_of_rendering(
    app_turn: AppTurn, case: str
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        if case == "threadless":
            turn.scope.thread_id = None
        elif case == "native":
            turn.scope.native = True
        elif case == "configured_sandbox":
            turn.host.config.artifacts.sandbox_origin = AnyHttpUrl(
                "http://localhost:8001"
            )
        elif case == "other_channel":
            other = FakeChannelTentacle(id=turn.channel.id, octomate=turn.host)
            await other.probe()
            turn.scope.channels[turn.channel.id] = other
        name = "ordinary" if case == "ordinary" else "draw"
        with turn.host.nerve.subscribe(turn.thread.key) as live:
            result = await turn.server.call_tool(
                CALL_MCP_TOOL,
                {
                    "namespace": turn.installation.namespace,
                    "name": name,
                    "arguments": {"xml": "<diagram />"},
                },
            )
            if case in {
                "unconfigured_sandbox",
                "configured_sandbox",
                "other_channel",
            }:
                assert isinstance((await live.receive()).event, ArtifactAvailableEvent)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(live.receive(), 0.01)
        assert result.structured_content == {"xml": "<diagram />", "ready": True}
        assert result.meta is not None
        assert result.meta["uiOnly"] == {"state": "preserved"}
        assert turn.ui_support == [True]


@pytest.mark.parametrize("operation", ["disable", "uninstall"])
async def test_pool_reuses_one_client_and_removes_it(
    app_turn: AppTurn, operation: str
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        manager = turn.host.mcp
        async with manager.acquire(turn.user, mcp_id=turn.installation.id) as first:
            await first.call_tool_mcp("draw", {"xml": "<first />"})
        await manager.catalog(turn.scope, turn.installation.namespace)
        async with manager.acquire(turn.scope, turn.installation.namespace) as second:
            assert first is second
            await second.call_tool_mcp("draw", {"xml": "<second />"})
        assert set(manager.clients) == {
            McpClientKey(turn.user.id, turn.installation.id),
        }
        assert turn.ui_support == [True, True]
        if operation == "disable":
            await manager.disable(turn.user.id, turn.installation.id)
        else:
            await manager.uninstall(turn.user.id, turn.installation.id)
        assert manager.clients == {}
        assert manager.tool_catalogs == {}
        assert manager.sweaps == {}
        assert not first.is_connected()


async def test_installation_id_lookup_never_uses_a_replacement(
    app_turn: AppTurn,
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        manager = turn.host.mcp
        other = await a_user("other")
        with pytest.raises(McpUnavailable):
            async with manager.acquire(other, mcp_id=turn.installation.id):
                pytest.fail("another user acquired the installation")
        await manager.uninstall(turn.user.id, turn.installation.id)
        await manager.install(
            turn.user.id,
            McpInstallRequest(
                name="Replacement",
                namespace="diagrams",
                url=AnyHttpUrl("https://replacement.example/mcp"),
            ),
        )
        with pytest.raises(McpUnavailable):
            async with manager.acquire(turn.user, mcp_id=turn.installation.id):
                pytest.fail("a removed installation resolved by namespace")
        assert turn.calls == []


async def test_storage_failure_does_not_publish_or_repeat_tool(
    app_turn: AppTurn, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        monkeypatch.setattr(
            turn.host.threads,
            "record_artifact",
            AsyncMock(side_effect=RuntimeError("storage refused")),
        )
        with turn.host.nerve.subscribe(turn.thread.key) as live:
            with pytest.raises(RuntimeError, match="storage refused"):
                await turn.host.mcp.call_tool(
                    turn.scope, turn.installation.namespace, "draw", {"xml": "once"}
                )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(live.receive(), 0.01)
        assert turn.calls == ["once"]


async def test_invalid_ui_declaration_is_rejected_before_execution(
    app_turn: AppTurn,
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        with pytest.raises(ValidationError, match="URL scheme"):
            await turn.server.call_tool(
                CALL_MCP_TOOL,
                {
                    "namespace": turn.installation.namespace,
                    "name": "invalid",
                    "arguments": {"xml": "never"},
                },
            )
        assert turn.calls == []


async def test_ledger_http_replay_exposes_only_references_without_tool_calls(
    app_turn: AppTurn,
) -> None:
    turn = app_turn
    async with connected(turn.host.mcp, turn.upstream):
        await turn.host.mcp.call_tool(
            turn.scope, turn.installation.namespace, "draw", {"xml": "saved input"}
        )
    turn.host.dependency_overrides[current_user] = lambda: turn.user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=turn.host), base_url="http://localhost"
    ) as client:
        for _ in range(2):
            response = await client.get(
                f"/api/trunkline/threads/{turn.thread.id}/messages",
                params={"unbound": "true", "limit": 1},
            )
            assert response.status_code == 200
            [entry] = response.json()["items"]
            assert entry["kind"] == "artifact"
            assert entry["artifact_id"] != entry["id"]
            assert "artifact" not in entry
            assert "app_invocation" not in entry
            assert "uiOnly" not in response.text
            assert "saved input" not in response.text
    assert turn.calls == ["saved input"]
