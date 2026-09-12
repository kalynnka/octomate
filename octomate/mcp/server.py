"""Compose gateway, history, and user-installed MCP tools under one server.

Runtimes use the same discovery and call helpers for every installed MCP.
Inkling already has gateway and history tools in process, so it mounts only
`tentacles_mcp`; the other runtimes mount the combined server.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import httpx2
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.shared.exceptions import MCPError
from pydantic import Field, JsonValue

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
from octomate.schemas.mcp import (
    McpInstallRequest,
    McpServerSummary,
    McpTentacleInfo,
    McpToolCatalog,
    NoAuth,
    OAuth,
)
from octomate.schemas.user import User
from octomate.types.oauth import HttpsUrl

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
LIST_MCP_TENTACLES = "mcp_list_tentacles"
INSTALL_MCP = "mcp_install"
UNINSTALL_MCP = "mcp_uninstall"
ENABLE_MCP = "mcp_enable"
DISABLE_MCP = "mcp_disable"


def gateway_tool(name: str) -> str:
    """A spell's served name: the family's namespace over Inkling's own."""
    return f"{GATEWAY_NAMESPACE}_{name}"


def history_tool(name: str) -> str:
    """A history tool's served name, from the capability's own."""
    return f"{HISTORY_NAMESPACE}_{HISTORY_TOOL_NAMES[name]}"


def tentacle_instructions() -> str:
    return (
        f"Call `{LIST_MCPS}` to discover the current user's installed MCPs. "
        f"Use `{LIST_MCP_TOOLS}` with an enabled namespace, then `{CALL_MCP_TOOL}` "
        "with the exact discovered tool name and arguments. "
        f"When the user asks to install an MCP, use `{LIST_MCP_TENTACLES}` to find "
        f"configured offerings, then `{INSTALL_MCP}`. Remote MCPs can also be "
        "installed by HTTPS URL with no authentication or dynamic OAuth. "
        f"Use `{ENABLE_MCP}`, `{DISABLE_MCP}`, and `{UNINSTALL_MCP}` with an "
        "installation's id when the user requests those changes. "
        "If authorization is required, use `oauth_connect` with that namespace "
        "and an available OAuth flow to send the user a private authorization "
        "link, then `oauth_confirm`. Installing does not authorize an MCP. "
        "OAuth links require a channel with private delivery. Enter personal "
        "bearer tokens through the authenticated MCP HTTP API, never tool arguments."
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
    session = Depends(resolve_session)
    mount_oauth(oauth, session, manager=manager)
    mcp.mount(oauth, namespace=OAUTH_NAMESPACE)

    async def require_user(
        scope: OctomateSession = session,
    ) -> User:
        if scope.user_profile is not None:
            user = await manager.users.owner(scope.user_profile)
            if user is not None:
                return user
        raise ToolError("MCP management requires a registered user")

    @mcp.tool(
        name=LIST_MCP_TENTACLES,
        description="List configured MCP tentacles the current user can install.",
        annotations={"readOnlyHint": True},
    )
    async def list_tentacles(
        # Depends declares injection; FastMCP resolves it separately for each call.
        user: User = Depends(require_user),  # noqa: B008
    ) -> list[McpTentacleInfo]:
        return manager.available()

    @mcp.tool(
        name=LIST_MCPS,
        description=(
            "List the current user's installed MCPs, including disabled ones, "
            "stored OAuth status, and available authorization flows. Does not poll providers."
        ),
        annotations={"readOnlyHint": True},
    )
    async def list_servers() -> list[McpServerSummary]:
        scope = await resolve_session()
        if scope.user_profile is None:
            return []
        owner = await manager.users.owner(scope.user_profile)
        if owner is None:
            return []
        return [
            await manager.summary(owner, instance)
            for instance in await manager.list(user_id=owner.id)
        ]

    @mcp.tool(
        name=INSTALL_MCP,
        description=(
            "Install an MCP for the current user. For a configured tentacle, pass its "
            "id and URL from mcp_list_tentacles and omit auth. For a remote URL, "
            "choose none or oauth. Namespace is a unique local name without personal/. "
            "Installation enables the MCP; OAuth authorization is a separate step."
        ),
        annotations={"destructiveHint": False},
    )
    async def install(
        name: str,
        namespace: str,
        url: HttpsUrl,
        *,
        tentacle_id: str | None = None,
        auth: Literal["none", "oauth"] | None = None,
        user: User = Depends(require_user),  # noqa: B008
    ) -> McpServerSummary:
        match auth:
            case "none":
                authentication = NoAuth()
            case "oauth":
                authentication = OAuth()
            case None:
                authentication = None
        try:
            instance = await manager.install(
                user_id=user.id,
                request=McpInstallRequest(
                    name=name,
                    namespace=namespace,
                    url=url,
                    tentacle_id=tentacle_id,
                    auth=authentication,
                ),
            )
        except (McpUnavailable, ValueError) as error:
            raise ToolError(str(error)) from error
        return await manager.summary(user, instance)

    @mcp.tool(
        name=ENABLE_MCP,
        description="Enable one of the current user's installed MCPs, keeping its saved authorization.",
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    async def enable(
        mcp_id: uuid.UUID,
        user: User = Depends(require_user),  # noqa: B008
    ) -> McpServerSummary:
        try:
            instance = await manager.enable(user_id=user.id, mcp_id=mcp_id)
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        return await manager.summary(user, instance)

    @mcp.tool(
        name=DISABLE_MCP,
        description=(
            "Disable one of the current user's MCPs and close its cached client "
            "immediately. Keeps the installation and saved authorization."
        ),
        annotations={"destructiveHint": False, "idempotentHint": True},
    )
    async def disable(
        mcp_id: uuid.UUID,
        user: User = Depends(require_user),  # noqa: B008
    ) -> McpServerSummary:
        try:
            instance = await manager.disable(user_id=user.id, mcp_id=mcp_id)
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        return await manager.summary(user, instance)

    @mcp.tool(
        name=UNINSTALL_MCP,
        description=(
            "Remove one of the current user's MCP installations and its saved "
            "credentials, closing its cached client immediately. Does not revoke "
            "authorization at the provider."
        ),
        annotations={"destructiveHint": True},
    )
    async def uninstall(
        mcp_id: uuid.UUID,
        user: User = Depends(require_user),  # noqa: B008
    ) -> str:
        try:
            await manager.uninstall(user_id=user.id, mcp_id=mcp_id)
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        return "MCP uninstalled."

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
