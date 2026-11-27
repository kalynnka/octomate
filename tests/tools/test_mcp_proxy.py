from __future__ import annotations

import asyncio

import pytest
from arcanus import Relation
from fastmcp import Client
from fastmcp.exceptions import NotFoundError, ToolError
from mcp.types import ServerNotification, TextContent, Tool, ToolListChangedNotification
from pydantic import TypeAdapter
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
