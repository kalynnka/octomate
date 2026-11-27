from __future__ import annotations

import asyncio

import httpx2
import pytest
from arcanus import Relation
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.dependencies import get_context, get_http_headers
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool as FastMcpTool
from fastmcp.tools import ToolResult
from fastmcp.tools.base import RawStructuredToolResult
from mcp.types import ServerNotification, TextContent, Tool, ToolListChangedNotification
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.mcp.proxy import SEARCH_MCP_TOOLS
from octomate.mcp.server import (
    DISABLE_MCP,
    ENABLE_MCP,
    INSTALL_MCP,
    UNINSTALL_MCP,
    tentacles_mcp,
)
from octomate.schemas.mcp import McpServerSummary
from octomate.schemas.user import UserProfile
from tests.support.managers import fixed_session
from tests.support.users import a_user
from tests.tools.test_mcp import ENCRYPTION_KEY, a_turn, an_upstream
from tests.tools.test_mcp_manager import connected, install_request

TOOL_LIST = TypeAdapter(list[Tool])


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


async def test_same_host_installations_keep_distinct_names_and_credentials() -> None:
    host = Octomate(oauth_encryption_key=ENCRYPTION_KEY)
    alice, bob = await a_user("alice"), await a_user("bob")
    alice_scope, bob_scope = [
        a_turn(UserProfile(user_id=user.id, user=Relation(user)))
        for user in (alice, bob)
    ]
    alice_server, bob_server = [
        tentacles_mcp(fixed_session(scope), manager=host.mcp)
        for scope in (alice_scope, bob_scope)
    ]
    for namespace, token in (("work", "first"), ("work_space", "second")):
        await host.mcp.install(
            alice.id, install_request(token).model_copy(update={"namespace": namespace})
        )
    upstream, credentials = an_upstream("answer")

    @upstream.tool(description="A tool whose name overlaps another installation.")
    def space_answer() -> str:
        return "separate tool"

    async with connected(host.mcp, upstream):
        names = {tool.name for tool in await alice_server.list_tools()}
        assert {
            "personal-work_answer",
            "personal-work_space_answer",
            "personal-work.space_answer",
            "personal-work.space_space_answer",
        } <= names
        await alice_server.call_tool("personal-work_answer", {})
        await alice_server.call_tool("personal-work.space_answer", {})
        assert credentials == ["Bearer first", "Bearer second"]
        assert not any(
            tool.name.startswith("personal-") for tool in await bob_server.list_tools()
        )
        search = await bob_server.call_tool(SEARCH_MCP_TOOLS, {"pattern": "personal-"})
        assert search.content == []
        with pytest.raises(NotFoundError):
            await bob_server.call_tool("personal-work_answer", {})


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"type": ["object", "null"]}, {"answer": "found"}),
        ({"type": ["null", "array"], "items": {"type": "string"}}, ["found"]),
        ({"oneOf": [{"type": "object"}, {"type": "null"}]}, {"answer": "found"}),
    ],
)
async def test_proxy_mirrors_protocol_for_discovery_and_calls(
    schema: dict[str, JsonValue], value: JsonValue
) -> None:
    host = Octomate()
    user = await a_user()
    scope = a_turn(UserProfile(user_id=user.id, user=Relation(user)))
    await host.mcp.install(user.id, install_request())
    server = tentacles_mcp(fixed_session(scope), manager=host.mcp)
    calls: list[str] = []
    legacy_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"result": schema},
        "required": ["result"],
    }

    def answer(ctx: Context) -> ToolResult:
        assert ctx.request_context is not None
        protocol = ctx.request_context.protocol_version
        calls.append(protocol)
        headers = get_http_headers(include={"authorization", "x-octomate-private"})
        assert "authorization" not in headers
        assert "x-octomate-private" not in headers
        if protocol == "2026-07-28":
            return RawStructuredToolResult(content=[], structured_content=value)
        return ToolResult(structured_content={"result": value})

    class ProtocolTools(Provider):
        async def get_tasks(self) -> list[FastMcpTool]:
            return []

        async def _list_tools(self) -> list[FastMcpTool]:
            context = get_context().request_context
            assert context is not None
            return [
                FastMcpTool.from_function(answer, output_schema=None).model_copy(
                    update={
                        "output_schema": (
                            schema
                            if context.protocol_version == "2026-07-28"
                            else legacy_schema
                        )
                    }
                )
            ]

    upstream = FastMCP("versioned-tools", providers=[ProtocolTools()])
    app = server.http_app(stateless_http=True, json_response=True)
    async with (
        connected(host.mcp, upstream),
        Client(server, mode="legacy", cache=False) as legacy,
        Client(server, mode="2026-07-28", cache=False) as modern,
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://octomate",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": "Bearer frontend-only",
                "X-Octomate-Private": "frontend-only",
            },
        ) as older,
    ):
        for client, expected_schema, expected_value in (
            (modern, schema, value),
            (legacy, legacy_schema, {"result": value}),
            (modern, schema, value),
        ):
            tools = await client.list_tools()
            tool = next(t for t in tools if t.name == "personal-research_answer")
            assert tool.output_schema == expected_schema
            result = await client.call_tool(tool.name, {})
            assert result.structured_content == expected_value
        assert calls == ["2026-07-28", "2025-11-25", "2026-07-28"]
        listed = await older.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert listed.status_code == 200
        tool = next(
            t
            for t in listed.json()["result"]["tools"]
            if t["name"] == "personal-research_answer"
        )
        assert tool["outputSchema"] == legacy_schema
        called = await older.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool["name"], "arguments": {}},
            },
        )
        assert called.status_code == 200
        assert called.json()["result"]["structuredContent"] == {"result": value}
        assert calls[-1] == "2025-11-25"


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_catalog_and_search_follow_changes_without_invocation_wrappers(
    mode: str,
) -> None:
    host = Octomate()
    user = await a_user()
    scope = a_turn(UserProfile(user_id=user.id, user=Relation(user)))
    server = tentacles_mcp(fixed_session(scope), manager=host.mcp)
    upstream, calls = an_upstream("answer")
    async with (
        connected(host.mcp, upstream),
        Client(server, mode=mode, cache=False) as client,
    ):
        initial = {tool.name for tool in await client.list_tools()}
        assert SEARCH_MCP_TOOLS in initial
        assert "mcp_call_tool" not in initial
        assert "call_tool" not in initial
        for wrapper in ("mcp_call_tool", "call_tool"):
            with pytest.raises(ToolError, match="Unknown tool"):
                await client.call_tool(wrapper, {"name": "answer", "arguments": {}})

        instance = await host.mcp.install(user.id, install_request())
        assert {tool.name for tool in await client.list_tools()} == {
            *initial,
            "personal-research_answer",
        }

        @upstream.tool(
            title="Another tool",
            description="Find a newly added capability.",
            annotations={"readOnlyHint": True},
            meta={"provider/detail": {"preserved": True}},
        )
        def another(query: str) -> dict[str, str]:
            return {"query": query}

        before = await client.list_tools()
        found = await client.call_tool(
            SEARCH_MCP_TOOLS, {"pattern": "personal-.*another"}
        )
        assert isinstance(found.content[0], TextContent)
        [definition] = TOOL_LIST.validate_json(found.content[0].text)
        assert definition == next(
            t for t in before if t.name == "personal-research_another"
        )
        source = await upstream.get_tool("another")
        assert source is not None
        assert definition == source.to_mcp_tool().model_copy(
            update={"name": "personal-research_another"}
        )
        assert await client.list_tools() == before
        assert calls == []
        result = await client.call_tool(definition.name, {"query": "specific"})
        assert result.structured_content == {"query": "specific"}

        upstream.disable(names={"another"})
        assert {tool.name for tool in await client.list_tools()} == {
            *initial,
            "personal-research_answer",
        }
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool(definition.name, {"query": "removed"})
        await host.mcp.disable(user.id, instance.id)
        assert {tool.name for tool in await client.list_tools()} == initial
        await host.mcp.enable(user.id, instance.id)
        assert "personal-research_answer" in {
            tool.name for tool in await client.list_tools()
        }
        await host.mcp.uninstall(user.id, instance.id)
        assert {tool.name for tool in await client.list_tools()} == initial


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_management_emits_only_supported_list_change_notifications(
    mode: str,
) -> None:
    host = Octomate()
    user = await a_user()
    scope = a_turn(UserProfile(user_id=user.id, user=Relation(user)))
    server = tentacles_mcp(fixed_session(scope), manager=host.mcp)
    upstream, _ = an_upstream("answer")
    changed = asyncio.Event()

    async def receive(message: ServerNotification | Exception) -> None:
        if isinstance(message, ToolListChangedNotification):
            changed.set()

    async with (
        connected(host.mcp, upstream),
        Client(server, mode=mode, message_handler=receive) as client,
    ):
        result = await client.call_tool(
            INSTALL_MCP,
            {
                "name": "Research",
                "namespace": "research",
                "url": "https://mcp.example/mcp",
            },
        )
        installed = McpServerSummary.model_validate(result.structured_content)
        if mode == "legacy":
            await asyncio.wait_for(changed.wait(), timeout=1)
        else:
            assert not changed.is_set()
        for action in (DISABLE_MCP, ENABLE_MCP, UNINSTALL_MCP):
            changed.clear()
            await client.call_tool(action, {"mcp_id": str(installed.id)})
            if mode == "legacy":
                await asyncio.wait_for(changed.wait(), timeout=1)
            else:
                assert not changed.is_set()
