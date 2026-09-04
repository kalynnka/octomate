"""Octomate's MCP server projected into Claude's native tool mechanism.

A driven Claude run mounts the served server — the gateway's spells and the
history tools — in process, with the turn's
`GatewaySession` closed over — identity by closure, so nothing on the wire names a
session, and the stdio control protocol carries the calls over an SSH transport
unchanged. The server is the same one `/gateway/mcp` serves; this module only walks
its tools into the SDK's own tool shape and translates their results back.
"""

from __future__ import annotations

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from pydantic import JsonValue, ValidationError

from octomate.capabilities.gateway import gateway_instructions
from octomate.capabilities.history import history_instructions
from octomate.managers.gateway import GatewaySession
from octomate.managers.thread import ThreadManager
from octomate.mcp.gateway import GATEWAY_SERVER_NAME
from octomate.mcp.server import octomate_mcp


def gateway_tool_name(name: str) -> str:
    """How Claude names an MCP tool: `mcp__<server>__<tool>`."""
    return f"mcp__{GATEWAY_SERVER_NAME}__{name}"


# The shared routing contract and the history one, rendered under Claude's tool
# naming.
GATEWAY_MCP_INSTRUCTION = (
    gateway_instructions(gateway_tool_name)
    + "\n"
    + history_instructions(gateway_tool_name)
)


def sdk_gateway_tool(tool: Tool) -> SdkMcpTool[dict[str, JsonValue]]:
    """One of the gateway's tools as the SDK's in-process server takes it."""
    if tool.description is None:
        raise RuntimeError(f"the gateway's `{tool.name}` has no contract to project")

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


async def gateway_mcp_server(
    session: GatewaySession,
    thread_manager: ThreadManager,
) -> McpSdkServerConfig:
    """The served server, mounted in process for this turn: every call runs against
    `session`, and a delivering spell writes through `thread_manager`, which the
    history tools read."""
    server = octomate_mcp(Depends(lambda: session), thread_manager)
    return create_sdk_mcp_server(
        GATEWAY_SERVER_NAME,
        tools=[sdk_gateway_tool(tool) for tool in await server.list_tools()],
    )
