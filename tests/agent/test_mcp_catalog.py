from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp.types import ListToolsRequest
from pydantic import SecretStr

from octomate import Octomate
from octomate.config import BareMcpConfig
from octomate.managers.gateway import OctomateSession
from octomate.mcp.server import (
    CALL_MCP_TOOL,
    LIST_MCP_TOOLS,
    octomate_mcp,
    tentacles_mcp,
)
from octomate.oauth.base import McpConnectionAuth
from octomate.tentacles import mcp
from tests.agent.test_mcp import a_turn, an_upstream, upstream_of
from tests.channels.slack.test_mcp import into
from tests.support.managers import FakeThreadManager, fixed_session
from tests.support.mcp import discover


class CatalogRequests(Middleware):
    bearers: list[str]

    def __init__(self) -> None:
        self.bearers = []

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        self.bearers.append(get_http_headers(include_all=True)["authorization"])
        return await call_next(context)


@pytest.mark.parametrize("empty", [False, True])
async def test_catalog_reused_across_concurrent_and_later_mounts(empty: bool) -> None:
    tentacle = mcp.BareMcpTentacle(
        "provider",
        Octomate(),
        config=BareMcpConfig(url="https://mcp.example/mcp", token=SecretStr("alice")),
    )
    upstream = FastMCP("empty") if empty else an_upstream("answer")[0]
    requests = CatalogRequests()
    upstream.add_middleware(requests)

    async with upstream_of(upstream) as transport:
        mounts = [
            tentacles_mcp(
                fixed_session(a_turn()),
                [tentacle],
                httpx_client_factory=into(transport),
            )
            for _ in range(3)
        ]
        for mount in mounts:
            assert [tool.name for tool in await mount.list_tools()] == [
                LIST_MCP_TOOLS,
                CALL_MCP_TOOL,
            ]
        assert requests.bearers == []
        first, concurrent = await asyncio.gather(
            discover(mounts[0], "provider"), discover(mounts[1], "provider")
        )
        later = await discover(mounts[2], "provider")

    assert requests.bearers == ["Bearer alice"]
    expected = [] if empty else ["provider_answer"]
    assert [
        [tool.name for tool in catalog.tools] for catalog in (first, concurrent, later)
    ] == [
        expected,
        expected,
        expected,
    ]


async def test_catalog_uses_current_credentials_and_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tentacle = mcp.BareMcpTentacle(
        "provider",
        Octomate(),
        config=BareMcpConfig(url="https://mcp.example/mcp", token=SecretStr("alice")),
    )
    alice, bob = a_turn(), a_turn()
    alice_token: SecretStr | None = SecretStr("alice")

    async def auth(session: OctomateSession) -> McpConnectionAuth:
        token = alice_token if session is alice else SecretStr("bob")
        if token is None:
            raise ToolError("Account unlinked")
        return McpConnectionAuth(token, tentacle.unauthorized)

    monkeypatch.setattr(tentacle, "auth", auth)
    upstream, calls = an_upstream("answer")
    requests = CatalogRequests()
    upstream.add_middleware(requests)

    async with upstream_of(upstream) as transport:
        first, second = [
            tentacles_mcp(
                fixed_session(session),
                [tentacle],
                httpx_client_factory=into(transport),
            )
            for session in (alice, bob)
        ]
        await discover(first, "provider")
        await discover(second, "provider")
        alice_token = SecretStr("alice-refreshed")
        await discover(first, "provider")
        assert requests.bearers == [
            "Bearer alice",
            "Bearer bob",
            "Bearer alice-refreshed",
        ]
        arguments = {
            "namespace": "provider",
            "name": "provider_answer",
            "arguments": {},
        }
        await second.call_tool(CALL_MCP_TOOL, arguments)
        await first.call_tool(CALL_MCP_TOOL, arguments)
        alice_token = None
        with pytest.raises(ToolError, match="Account unlinked"):
            await discover(first, "provider")
        with pytest.raises(ToolError, match="Account unlinked"):
            await first.call_tool(CALL_MCP_TOOL, arguments)

    assert calls == ["Bearer bob", "Bearer alice-refreshed"]


async def test_expired_catalog_fetches_changed_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp, "TOOL_CATALOG_TTL", 0)
    tentacle = mcp.BareMcpTentacle(
        "provider",
        Octomate(),
        config=BareMcpConfig(url="https://mcp.example/mcp", token=SecretStr("alice")),
    )
    upstream = FastMCP("upstream")
    requests = CatalogRequests()
    upstream.add_middleware(requests)

    async with upstream_of(upstream) as transport:
        server = tentacles_mcp(
            fixed_session(a_turn()), [tentacle], httpx_client_factory=into(transport)
        )
        assert (await discover(server, "provider")).tools == []

        @upstream.tool
        def added() -> str:
            """A newly available tool."""
            return "ok"

        catalog = await discover(server, "provider")
        assert [tool.name for tool in catalog.tools] == ["provider_added"]

    assert requests.bearers == ["Bearer alice", "Bearer alice"]


async def test_initial_listing_never_resolves_provider_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tentacle = mcp.BareMcpTentacle(
        "offline",
        Octomate(),
        config=BareMcpConfig(url="https://offline.example/mcp", token=SecretStr("key")),
    )

    async def unavailable(session: OctomateSession) -> McpConnectionAuth:
        raise AssertionError("Initial listing must not resolve upstream credentials")

    monkeypatch.setattr(tentacle, "auth", unavailable)
    server = octomate_mcp(
        fixed_session(a_turn()), FakeThreadManager(), tentacles=[tentacle]
    )
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {
            "gateway_send",
            "history_search",
            LIST_MCP_TOOLS,
            CALL_MCP_TOOL,
        } <= names
        assert all(name.startswith(("gateway_", "history_", "mcp_")) for name in names)
        with pytest.raises(ToolError, match="Unknown MCP namespace"):
            await discover(client, "missing")
        with pytest.raises(ToolError, match="Unknown MCP namespace"):
            await client.call_tool(
                CALL_MCP_TOOL,
                {"namespace": "missing", "name": "answer", "arguments": {}},
            )
        with pytest.raises(ToolError, match="No tool 'different_answer'"):
            await client.call_tool(
                CALL_MCP_TOOL,
                {"namespace": "offline", "name": "different_answer", "arguments": {}},
            )


async def test_discovery_and_calls_stay_in_the_selected_namespace() -> None:
    host = Octomate()
    tentacles = [
        mcp.BareMcpTentacle(
            namespace,
            host,
            config=BareMcpConfig(
                url="https://mcp.example/mcp", token=SecretStr(namespace)
            ),
        )
        for namespace in ("first", "second")
    ]
    # Providers like Slack supply their own names; namespaces must still isolate them.
    for tentacle in tentacles:
        tentacle.prefix = None
        tentacle.instructions = f"Instructions for {tentacle.id}."
    upstream, calls = an_upstream("answer")
    requests = CatalogRequests()
    upstream.add_middleware(requests)

    async with upstream_of(upstream) as transport:
        server = tentacles_mcp(
            fixed_session(a_turn()), tentacles, httpx_client_factory=into(transport)
        )
        async with Client(server) as client:
            initial = await client.list_tools()
            assert requests.bearers == []
            catalog = await discover(client, "second")
            assert requests.bearers == ["Bearer second"]
            assert catalog.instructions == "Instructions for second."
            assert [tool.name for tool in catalog.tools] == ["answer"]
            assert catalog.tools[0].description == "What the provider says of its tool."
            assert catalog.tools[0].input_schema["type"] == "object"
            assert [tool.name for tool in await client.list_tools()] == [
                tool.name for tool in initial
            ]
            assert requests.bearers == ["Bearer second"]
            for namespace in ("second", "first"):
                result = await client.call_tool(
                    CALL_MCP_TOOL,
                    {"namespace": namespace, "name": "answer", "arguments": {}},
                )
                assert result.data == "answered"

    assert calls == ["Bearer second", "Bearer first"]
