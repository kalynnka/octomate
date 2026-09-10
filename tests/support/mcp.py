from fastmcp import Client, FastMCP

from octomate.mcp.server import LIST_MCP_TOOLS, McpToolCatalog


async def discover(server: Client | FastMCP, namespace: str) -> McpToolCatalog:
    result = await server.call_tool(LIST_MCP_TOOLS, {"namespace": namespace})
    return McpToolCatalog.model_validate(result.structured_content)
