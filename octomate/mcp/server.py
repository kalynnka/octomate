"""The one server Octomate serves and every runtime mounts: the gateway's spells,
the history tools, the account-linking tools, and helpers to discover and call
each MCP tentacle's tools on demand, composed here under one name.

One server rather than one per family because the served endpoint, Claude's
in-process mount and each runtime's install config all know one URL,
`/octomate/mcp`, and the server is named for it. Octomate's own families are
mounted under a namespace each — `gateway_send`, `history_search`,
`oauth_connect` — so a runtime that namespaces a server's tools reads
`mcp__octomate__gateway_send`, while an MCP tentacle's tools carry the
prefix it gives them. The instructions are composed here too: one
contract under the served names, whatever prefix a runtime lists them with — the
usual MCP arrangement, which every MCP tentacle's own instructions rely on
too.

The tentacles are a family a runtime may take on its own: Inkling, which has
the spells and the history in process already, mounts `tentacles_mcp` and
nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

import httpx2
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.providers import Provider
from fastmcp.tools import ToolResult
from mcp.shared._httpx_utils import McpHttpClientFactory
from mcp.shared.exceptions import MCPError
from mcp.types import Tool
from pydantic import BaseModel, Field, JsonValue

from octomate.capabilities.gateway import gateway_instructions
from octomate.capabilities.history import history_instructions
from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.managers.thread import ThreadManager
from octomate.mcp.base import KnownBearers
from octomate.mcp.gateway import mount_gateway
from octomate.mcp.history import HISTORY_TOOL_NAMES, mount_history
from octomate.mcp.oauth import OAUTH_NAMESPACE, mount_oauth, oauth_instructions
from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.tentacles.mcp import McpTentacle, OAuthMcpTentacle

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


class McpToolCatalog(BaseModel):
    instructions: str = Field(description="The selected provider's tool instructions.")
    tools: list[Tool] = Field(description="The selected provider's MCP tool schemas.")


def gateway_tool(name: str) -> str:
    """A spell's served name: the family's namespace over Inkling's own."""
    return f"{GATEWAY_NAMESPACE}_{name}"


def history_tool(name: str) -> str:
    """A history tool's served name, from the capability's own."""
    return f"{HISTORY_NAMESPACE}_{HISTORY_TOOL_NAMES[name]}"


def tentacle_instructions(
    tentacles: Sequence[McpTentacle], *, personal: bool = False
) -> str:
    """Discovery and linking instructions, without fetching upstream catalogs."""
    linkable = [t for t in tentacles if isinstance(t, OAuthMcpTentacle)]
    parts = [oauth_instructions(linkable)] if linkable else []
    if tentacles:
        namespaces = ", ".join(f"`{t.id}` ({t.label})" for t in tentacles)
        parts.append(
            f"Provider tools are loaded on demand. Available namespaces: {namespaces}. "
            f"Call `{LIST_MCP_TOOLS}` with a namespace to read its instructions and "
            f"tool schemas, then `{CALL_MCP_TOOL}` with that namespace, the exact "
            "listed tool name, and its arguments."
        )
    if personal:
        parts.append(
            f"Call `{LIST_MCPS}` to discover the current user's installed MCPs. "
            f"Use `{LIST_MCP_TOOLS}` with a returned namespace, then `{CALL_MCP_TOOL}` "
            "with the exact listed tool name and arguments."
        )
    return "\n".join(parts)


def octomate_instructions(
    tentacles: Sequence[McpTentacle], *, personal: bool = False
) -> str:
    """The server's instructions: every family's contract under the served names."""
    parts = [gateway_instructions(gateway_tool), history_instructions(history_tool)]
    if tentacles or personal:
        parts.append(tentacle_instructions(tentacles, personal=personal))
    return "\n".join(parts)


def tentacles_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    tentacles: Sequence[McpTentacle],
    *,
    httpx_client_factory: McpHttpClientFactory | None = None,
    manager: McpManager | None = None,
) -> FastMCP:
    """The tentacles as a server of their own: every one of `tentacles` listing
    and calling as the caller `resolve_session` resolves, and the link tools for
    those that link. The served server mounts it beside Octomate's own families;
    a runtime that already has the spells and the history in process mounts it
    alone. `httpx_client_factory` is how a test stands in for a tentacle's
    upstream."""
    mcp = FastMCP(
        name=TENTACLES_SERVER_NAME,
        instructions=tentacle_instructions(tentacles, personal=manager is not None),
    )
    linkable = [t for t in tentacles if isinstance(t, OAuthMcpTentacle)]
    if linkable:
        oauth = FastMCP(OAUTH_NAMESPACE)
        mount_oauth(oauth, Depends(resolve_session), linkable)
        mcp.mount(oauth, namespace=OAUTH_NAMESPACE)
    if not tentacles and manager is None:
        return mcp
    if manager is not None:

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

    namespaces = {
        tentacle.id: (
            tentacle,
            tentacle.provider(
                resolve_session, httpx_client_factory=httpx_client_factory
            ),
        )
        for tentacle in tentacles
    }
    names = ", ".join(
        f"`{id}` ({tentacle.label})" for id, (tentacle, _) in namespaces.items()
    )

    def named(namespace: str) -> tuple[McpTentacle, Provider]:
        found = namespaces.get(namespace)
        if found is None:
            raise ToolError(f"Unknown MCP namespace {namespace!r}; available: {names}.")
        return found

    @mcp.tool(
        name=LIST_MCP_TOOLS,
        description=f"Load one provider's tool schemas and instructions. Namespaces: {names}.",
    )
    async def list_tools(namespace: str) -> McpToolCatalog:
        if manager is not None and namespace.startswith("personal/"):
            try:
                async with manager.acquire(
                    await resolve_session(), namespace
                ) as client:
                    return McpToolCatalog(
                        instructions=client.instructions or "",
                        tools=await client.list_tools(),
                    )
            except McpUnavailable as error:
                raise ToolError(str(error)) from error
            except (httpx2.HTTPError, MCPError, TimeoutError) as error:
                raise ToolError("MCP upstream request failed") from error
        tentacle, provider = named(namespace)
        return McpToolCatalog(
            instructions=tentacle.instructions,
            tools=[tool.to_mcp_tool() for tool in await provider.list_tools()],
        )

    @mcp.tool(
        name=CALL_MCP_TOOL,
        description=(
            f"Call a tool discovered with `{LIST_MCP_TOOLS}` as the current caller. "
            f"Namespaces: {names}."
        ),
    )
    async def call_tool(
        namespace: str,
        name: Annotated[
            str, Field(description="The exact tool name returned by discovery.")
        ],
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        if manager is not None and namespace.startswith("personal/"):
            try:
                async with manager.acquire(
                    await resolve_session(), namespace
                ) as client:
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
        _, provider = named(namespace)
        tool = await provider.get_tool(name)
        if tool is None:
            raise ToolError(f"No tool {name!r} in MCP namespace {namespace!r}.")
        # Claude invokes SDK tool handlers directly, outside a FastMCP request.
        async with Context(mcp):
            return await tool.run(arguments)

    return mcp


def octomate_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    thread_manager: ThreadManager,
    kick: Callable[[GatewayHandoffSignal], None] | None = None,
    *,
    bearers: KnownBearers | None = None,
    tentacles: Sequence[McpTentacle] = (),
    httpx_client_factory: McpHttpClientFactory | None = None,
    manager: McpManager | None = None,
) -> FastMCP:
    """The server, built by whoever mounts it: `resolve_session` is the session a
    call runs against — one fixed turn for a server mounted in-process, a
    per-request lookup for the served one — `thread_manager` the ledger the spells
    write through and the history tools read, `kick` what a native session's
    summon or scheme needs to become its own turn (see `mount_gateway`),
    `bearers` the credentials a served endpoint answers to — none for a server
    mounted in-process, whose identity is by closure — and `tentacles` the MCP
    tentacles the server proxies, each listing and calling as the caller.
    `httpx_client_factory` is how a test stands in for a tentacle's upstream."""
    session = Depends(resolve_session)
    mcp = FastMCP(
        name=OCTOMATE_SERVER_NAME,
        instructions=octomate_instructions(tentacles, personal=manager is not None),
        auth=bearers,
    )
    gateway = FastMCP(GATEWAY_NAMESPACE)
    mount_gateway(gateway, session, thread_manager, kick)
    mcp.mount(gateway, namespace=GATEWAY_NAMESPACE)
    history = FastMCP(HISTORY_NAMESPACE)
    mount_history(history, session, thread_manager)
    mcp.mount(history, namespace=HISTORY_NAMESPACE)
    # Discovery, call, and linking helpers already carry their served names.
    mcp.mount(
        tentacles_mcp(
            resolve_session,
            tentacles,
            httpx_client_factory=httpx_client_factory,
            manager=manager,
        )
    )
    return mcp
