"""Compose gateway, history, and user-installed MCP tools under one server.

Runtimes use the same discovery and call helpers for every installed MCP.
Inkling already has gateway and history tools in process, so it mounts only
`tentacles_mcp`; the other runtimes mount the combined server.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx2
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, JsonValue

from octomate.capabilities.gateway import gateway_instructions
from octomate.capabilities.history import history_instructions
from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.managers.thread import ThreadManager
from octomate.mcp.base import KnownBearers
from octomate.mcp.gateway import mount_gateway
from octomate.mcp.history import HISTORY_TOOL_NAMES, mount_history
from octomate.mcp.oauth import OAUTH_NAMESPACE, mount_oauth
from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.schemas.mcp import McpToolCatalog

# The name every runtime mounts the server under. Claude and dsh name a server's
# tools `mcp__<server>__<tool>`, Codex namespaces them `mcp__<server>`.
OCTOMATE_SERVER_NAME = "octomate"
# The one endpoint: the host mounts the server's app under its name, and the
# transport answers at `/mcp` inside it. Every install config copies this literal.
OCTOMATE_MCP_PATH = f"/{OCTOMATE_SERVER_NAME}/mcp"
GATEWAY_NAMESPACE = "gateway"
HISTORY_NAMESPACE = "history"
TENTACLES_SERVER_NAME = "tentacles"
LIST_MCP_TOOLS = "mcp_list_tools"
CALL_MCP_TOOL = "mcp_call_tool"
LIST_MCPS = "mcp_list_servers"


class McpServerSummary(BaseModel):
    namespace: str
    name: str


def gateway_tool(name: str) -> str:
    """A spell's served name: the family's namespace over Inkling's own."""
    return f"{GATEWAY_NAMESPACE}_{name}"


def history_tool(name: str) -> str:
    """A history tool's served name, from the capability's own."""
    return f"{HISTORY_NAMESPACE}_{HISTORY_TOOL_NAMES[name]}"


def tentacle_instructions() -> str:
    return (
        f"Call `{LIST_MCPS}` to discover the current user's installed MCPs. "
        f"Use `{LIST_MCP_TOOLS}` with a returned namespace, then `{CALL_MCP_TOOL}` "
        "with the exact discovered tool name and arguments. "
        "If authorization is required, use `oauth_connect` with that namespace "
        "to send the user a private authorization link, then `oauth_confirm`."
    )


def octomate_instructions() -> str:
    return "\n".join(
        [
            gateway_instructions(gateway_tool),
            history_instructions(history_tool),
            tentacle_instructions(),
        ]
    )


def tentacles_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    *,
    manager: McpManager,
) -> FastMCP:
    """Serve discovery, calls, and authorization for the current user's MCPs."""
    mcp = FastMCP(
        name=TENTACLES_SERVER_NAME,
        instructions=tentacle_instructions(),
    )
    oauth = FastMCP(OAUTH_NAMESPACE)
    mount_oauth(oauth, Depends(resolve_session), manager=manager)
    mcp.mount(oauth, namespace=OAUTH_NAMESPACE)

    @mcp.tool(
        name=LIST_MCPS,
        description="Discover the current user's enabled MCP instances.",
    )
    async def list_servers() -> list[McpServerSummary]:
        scope = await resolve_session()
        if scope.user_profile is None:
            return []
        owner = await manager.users.owner(scope.user_profile)
        if owner is None:
            return []
        return [
            McpServerSummary(namespace=instance.namespace, name=instance.name)
            for instance in await manager.list(user_id=owner.id, enabled=True)
        ]

    @mcp.tool(
        name=LIST_MCP_TOOLS,
        description="Load tool schemas for one of the current user's installed MCPs.",
    )
    async def list_tools(namespace: str) -> McpToolCatalog:
        try:
            return await manager.catalog(await resolve_session(), namespace)
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        except (httpx2.HTTPError, MCPError, TimeoutError) as error:
            raise ToolError("MCP upstream request failed") from error

    @mcp.tool(
        name=CALL_MCP_TOOL,
        description=(
            f"Call a tool discovered with `{LIST_MCP_TOOLS}` as the current caller. "
        ),
    )
    async def call_tool(
        namespace: str,
        name: Annotated[
            str, Field(description="The exact tool name returned by discovery.")
        ],
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        try:
            async with manager.acquire(await resolve_session(), namespace) as client:
                result = await client.call_tool_mcp(name, arguments)
                return ToolResult(
                    content=result.content,
                    structured_content=result.structured_content,
                    is_error=result.is_error,
                )
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        except (httpx2.HTTPError, MCPError, TimeoutError) as error:
            raise ToolError("MCP upstream request failed") from error

    return mcp


def octomate_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    thread_manager: ThreadManager,
    kick: Callable[[GatewayHandoffSignal], None] | None = None,
    *,
    bearers: KnownBearers | None = None,
    manager: McpManager,
) -> FastMCP:
    """Compose the gateway, history, and user-scoped MCP tools over one session resolver."""
    session = Depends(resolve_session)
    mcp = FastMCP(
        name=OCTOMATE_SERVER_NAME,
        instructions=octomate_instructions(),
        auth=bearers,
    )
    gateway = FastMCP(GATEWAY_NAMESPACE)
    mount_gateway(gateway, session, thread_manager, kick)
    mcp.mount(gateway, namespace=GATEWAY_NAMESPACE)
    history = FastMCP(HISTORY_NAMESPACE)
    mount_history(history, session, thread_manager)
    mcp.mount(history, namespace=HISTORY_NAMESPACE)
    # Discovery, call, and linking helpers already carry their served names.
    mcp.mount(tentacles_mcp(resolve_session, manager=manager))
    return mcp
