"""Octomate's MCP server projected into Claude's native tool mechanism.

A driven Claude run mounts the served server — the gateway's spells, the history
tools, and lazy provider discovery and calls with the linking pair — in process,
with the turn's `OctomateSession` closed over — identity by closure, so nothing on
the wire names a session, and the stdio control protocol carries the calls over
an SSH transport unchanged. The server is the same one `/octomate/mcp` serves;
this module only walks its tools into the SDK's own tool shape and translates
their results back. The SDK's server carries no instructions of its own, so the
tentacle appends the server's to the system prompt instead — under the served
names, which Claude lists as `mcp__octomate__<tool>` and resolves itself, as it
does for every MCP server.
"""

from __future__ import annotations

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as McpValidationError
from fastmcp.tools import Tool
from pydantic import JsonValue, ValidationError

from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager
from octomate.managers.thread import ThreadManager
from octomate.mcp.server import OCTOMATE_SERVER_NAME, octomate_mcp


def sdk_tool(tool: Tool) -> SdkMcpTool[dict[str, JsonValue]]:
    """One of the server's tools as the SDK's in-process server takes it."""
    if tool.description is None:
        raise RuntimeError(f"the server's `{tool.name}` has no contract to project")

    async def handler(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            result = await tool.run(arguments)
        except (ToolError, McpValidationError, ValidationError) as refusal:
            # The same corrective sentence Inkling's ModelRetry carries, as a tool
            # error Claude retries from natively.
            return {
                "content": [{"type": "text", "text": str(refusal)}],
                "is_error": True,
            }
        response: dict[str, JsonValue] = {
            "content": [
                block.model_dump(mode="json", by_alias=True, exclude_none=True)
                for block in result.content
            ]
        }
        if result.is_error:
            response["is_error"] = True
        return response

    return SdkMcpTool(
        name=tool.name,
        description=tool.description,
        input_schema=tool.parameters,
        handler=handler,
    )


async def octomate_mcp_server(
    session: OctomateSession,
    thread_manager: ThreadManager,
    *,
    manager: McpManager,
) -> McpSdkServerConfig:
    """Mount the gateway, history, and the user's MCP tools in process for this turn."""

    async def fixed() -> OctomateSession:
        return session

    server = octomate_mcp(fixed, thread_manager, manager=manager)
    return create_sdk_mcp_server(
        OCTOMATE_SERVER_NAME,
        tools=[sdk_tool(tool) for tool in await server.list_tools()],
    )
