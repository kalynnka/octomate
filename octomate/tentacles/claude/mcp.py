"""Octomate's MCP server projected into Claude's native tool mechanism.

A driven Claude run mounts the served server — the gateway's spells, the history
tools, and every proxied provider's tools with the linking pair — in process,
with the turn's `GatewaySession` closed over — identity by closure, so nothing on
the wire names a session, and the stdio control protocol carries the calls over
an SSH transport unchanged. The server is the same one `/octomate/mcp` serves;
this module only walks its tools into the SDK's own tool shape and translates
their results back. The SDK's server carries no instructions of its own, so the
tentacle appends the server's to the system prompt instead — under the served
names, which Claude lists as `mcp__octomate__<tool>` and resolves itself, as it
does for every MCP server.
"""

from __future__ import annotations

from collections.abc import Sequence

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from pydantic import JsonValue, ValidationError

from octomate.managers.gateway import GatewaySession
from octomate.managers.thread import ThreadManager
from octomate.mcp.server import OCTOMATE_SERVER_NAME, octomate_mcp
from octomate.tentacles.mcp import McpTentacle


def sdk_tool(tool: Tool) -> SdkMcpTool[dict[str, JsonValue]]:
    """One of the server's tools as the SDK's in-process server takes it."""
    if tool.description is None:
        raise RuntimeError(f"the server's `{tool.name}` has no contract to project")

    async def handler(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            result = await tool.run(arguments)
        except (ToolError, ValidationError) as refusal:
            # The same corrective sentence Inkling's ModelRetry carries, as a tool
            # error Claude retries from natively.
            return {
                "content": [{"type": "text", "text": str(refusal)}],
                "is_error": True,
            }
        return {
            "content": [
                block.model_dump(mode="json", exclude_none=True)
                for block in result.content
            ]
        }

    return SdkMcpTool(
        name=tool.name,
        description=tool.description,
        input_schema=tool.parameters,
        handler=handler,
    )


async def octomate_mcp_server(
    session: GatewaySession,
    thread_manager: ThreadManager,
    tentacles: Sequence[McpTentacle] = (),
) -> McpSdkServerConfig:
    """The served server, mounted in process for this turn: every call runs
    against `session`, a delivering spell writes through `thread_manager`, which
    the history tools read, and `tentacles` are the providers the turn may
    reach — listed now, as the person the turn is for."""

    async def fixed() -> GatewaySession:
        return session

    server = octomate_mcp(fixed, thread_manager, tentacles=tentacles)
    return create_sdk_mcp_server(
        OCTOMATE_SERVER_NAME,
        tools=[sdk_tool(tool) for tool in await server.list_tools()],
    )
