import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.mcp import TentaclesToolset
from octomate.config import OctomateConfig
from octomate.mcp.gateway import CLIENT_HEADER
from octomate.mcp.server import CALL_MCP_TOOL, LIST_MCPS, tentacles_mcp
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude.mcp import octomate_mcp_server
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.agent.test_mcp import ENCRYPTION_KEY, a_turn, an_upstream, upstream_of
from tests.agent.test_mcp_serving import over, served
from tests.channels.slack.test_mcp import into
from tests.managers.test_mcp import install_request
from tests.support.managers import fixed_session
from tests.support.users import a_api_key, a_user, auth_config


async def test_served_endpoint_uses_authenticated_owner_and_sees_new_installs(
    in_memory_engine: AsyncEngine,
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    await a_api_key(alice, "alice-token")
    await a_api_key(bob, "bob-token")
    host = Octomate(
        config=OctomateConfig(auth=auth_config()), oauth_encryption_key=ENCRYPTION_KEY
    )
    upstream, calls = an_upstream("answer")
    async with upstream_of(upstream) as transport:
        host.mcp.httpx_client_factory = into(transport)
        async with served(host):
            async with (
                over(
                    host,
                    host,
                    {
                        "Authorization": "Bearer alice-token",
                        CLIENT_HEADER: CLAUDE_NATIVE_ID,
                    },
                ) as alice_client,
                over(
                    host,
                    host,
                    {
                        "Authorization": "Bearer bob-token",
                        CLIENT_HEADER: CLAUDE_NATIVE_ID,
                    },
                ) as bob_client,
            ):
                assert (await alice_client.call_tool(LIST_MCPS, {})).data == []
                instance = await host.mcp.install(
                    alice.id, install_request("alice-provider-secret")
                )
                assert "Private research" in str(
                    (await alice_client.call_tool(LIST_MCPS, {})).data
                )
                assert (await bob_client.call_tool(LIST_MCPS, {})).data == []
                args = {
                    "namespace": instance.namespace,
                    "name": "answer",
                    "arguments": {},
                }
                with pytest.raises(ToolError, match="unavailable"):
                    await bob_client.call_tool(CALL_MCP_TOOL, args)
                result = await alice_client.call_tool(CALL_MCP_TOOL, args)
                assert isinstance(result.content[0], TextContent)
                assert result.content[0].text == "answered"
                await host.mcp.disable(user_id=alice.id, mcp_id=instance.id)
                with pytest.raises(ToolError, match="unavailable"):
                    await alice_client.call_tool(CALL_MCP_TOOL, args)
    assert calls == ["Bearer alice-provider-secret"]


async def test_inkling_and_claude_mount_personal_discovery_without_tentacles(
    in_memory_engine: AsyncEngine,
) -> None:
    user = await a_user()
    host = Octomate(oauth_encryption_key=ENCRYPTION_KEY)
    scope = a_turn(UserProfile(user_id=user.id))
    await host.mcp.install(user.id, install_request())
    server = tentacles_mcp(fixed_session(scope), [], manager=host.mcp)
    toolset = TentaclesToolset(server)
    context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(context)
    listed = await toolset.call_tool(LIST_MCPS, {}, context, tools[LIST_MCPS])
    assert "Private research" in str(listed)
    claude = await octomate_mcp_server(scope, host.thread_manager, manager=host.mcp)
    assert claude["name"] == "octomate"
    assert "mcp_list_servers" in [tool.name for tool in await server.list_tools()]


async def test_upstream_tool_errors_are_retries_in_inkling(
    in_memory_engine: AsyncEngine,
) -> None:
    user = await a_user()
    host = Octomate()
    scope = a_turn(UserProfile(user_id=user.id))
    instance = await host.mcp.install(user.id, install_request())
    server = tentacles_mcp(fixed_session(scope), [], manager=host.mcp)
    toolset = TentaclesToolset(server)
    context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(context)
    upstream, _ = an_upstream("answer")
    async with upstream_of(upstream) as transport:
        host.mcp.httpx_client_factory = into(transport)
        async with host.mcp.lifespan():
            with pytest.raises(ModelRetry, match="Unknown tool"):
                await toolset.call_tool(
                    CALL_MCP_TOOL,
                    {
                        "namespace": instance.namespace,
                        "name": "missing",
                        "arguments": {},
                    },
                    context,
                    tools[CALL_MCP_TOOL],
                )
