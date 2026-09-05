"""The one server Octomate serves and every runtime mounts: the gateway's spells,
the history tools, the account-linking tools, and every MCP tentacle's own
tools, each family from its own module, composed here under one name.

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

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from mcp.shared._httpx_utils import McpHttpClientFactory

from octomate.capabilities.gateway import gateway_instructions
from octomate.capabilities.history import history_instructions
from octomate.managers.gateway import OctomateSession
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


def gateway_tool(name: str) -> str:
    """A spell's served name: the family's namespace over Inkling's own."""
    return f"{GATEWAY_NAMESPACE}_{name}"


def history_tool(name: str) -> str:
    """A history tool's served name, from the capability's own."""
    return f"{HISTORY_NAMESPACE}_{HISTORY_TOOL_NAMES[name]}"


def tentacle_instructions(tentacles: Sequence[McpTentacle]) -> str:
    """The linking contract, for the tentacles that link, and after it each
    tentacle's own contract — worded once, however many share it."""
    linkable = [t for t in tentacles if isinstance(t, OAuthMcpTentacle)]
    parts = [oauth_instructions(linkable)] if linkable else []
    parts.extend(dict.fromkeys(t.instructions for t in tentacles if t.instructions))
    return "\n".join(parts)


def octomate_instructions(tentacles: Sequence[McpTentacle]) -> str:
    """The server's instructions: every family's contract under the served names."""
    parts = [gateway_instructions(gateway_tool), history_instructions(history_tool)]
    if tentacles:
        parts.append(tentacle_instructions(tentacles))
    return "\n".join(parts)


def tentacles_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    tentacles: Sequence[McpTentacle],
    *,
    httpx_client_factory: McpHttpClientFactory | None = None,
) -> FastMCP:
    """The tentacles as a server of their own: every one of `tentacles` listing
    and calling as the caller `resolve_session` resolves, and the link tools for
    those that link. The served server mounts it beside Octomate's own families;
    a runtime that already has the spells and the history in process mounts it
    alone. `httpx_client_factory` is how a test stands in for a tentacle's
    upstream."""
    mcp = FastMCP(
        name=TENTACLES_SERVER_NAME, instructions=tentacle_instructions(tentacles)
    )
    linkable = [t for t in tentacles if isinstance(t, OAuthMcpTentacle)]
    if linkable:
        oauth = FastMCP(OAUTH_NAMESPACE)
        mount_oauth(oauth, Depends(resolve_session), linkable)
        mcp.mount(oauth, namespace=OAUTH_NAMESPACE)
    for tentacle in tentacles:
        mcp.add_provider(
            tentacle.provider(
                resolve_session, httpx_client_factory=httpx_client_factory
            )
        )
    return mcp


def octomate_mcp(
    resolve_session: Callable[[], Awaitable[OctomateSession]],
    thread_manager: ThreadManager,
    kick: Callable[[GatewayHandoffSignal], None] | None = None,
    *,
    bearers: KnownBearers | None = None,
    tentacles: Sequence[McpTentacle] = (),
    httpx_client_factory: McpHttpClientFactory | None = None,
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
        instructions=octomate_instructions(tentacles),
        auth=bearers,
    )
    gateway = FastMCP(GATEWAY_NAMESPACE)
    mount_gateway(gateway, session, thread_manager, kick)
    mcp.mount(gateway, namespace=GATEWAY_NAMESPACE)
    history = FastMCP(HISTORY_NAMESPACE)
    mount_history(history, session, thread_manager)
    mcp.mount(history, namespace=HISTORY_NAMESPACE)
    # No namespace: each tentacle's tools already carry the prefix it gives them,
    # and the link tools their own.
    mcp.mount(
        tentacles_mcp(
            resolve_session,
            tentacles,
            httpx_client_factory=httpx_client_factory,
        )
    )
    return mcp
