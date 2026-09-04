"""The one server Octomate serves and every runtime mounts: the gateway's spells and
the history tools, each family from its own module, composed here.

One server rather than one per family because the served endpoint, Claude's
in-process mount and each runtime's install config all know one URL, `/gateway/mcp`,
and the server is named for it. A runtime that namespaces a server's tools reads both
families under that name, and one that defers MCP tools behind a search shows this
server's instructions as the card for both — which is why the instructions are
composed here too, under the tools' bare names. Splitting the families into servers
of their own is the follow-up if this card gets crowded: the CLI's install entries,
the Codex launch overrides and the Claude mount would each learn a second name.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from octomate.capabilities.gateway import gateway_instructions
from octomate.capabilities.history import history_instructions
from octomate.managers.gateway import GatewaySession
from octomate.managers.thread import ThreadManager
from octomate.mcp.gateway import GATEWAY_SERVER_NAME, mount_gateway
from octomate.mcp.history import mount_history
from octomate.schemas.awakes import GatewayHandoffSignal

SERVER_INSTRUCTIONS = (
    gateway_instructions(lambda name: name)
    + "\n"
    + history_instructions(lambda name: name)
)


def octomate_mcp(
    gateway_session: GatewaySession,
    thread_manager: ThreadManager,
    kick: Callable[[GatewayHandoffSignal], None] | None = None,
) -> FastMCP:
    """The server, built by whoever mounts it: `gateway_session` resolves the session a
    call runs against, `thread_manager` is the ledger the spells write through and
    the history tools read, and `kick` is what a native session's summon or scheme
    needs to become its own turn — see `mount_gateway`."""
    mcp = FastMCP(name=GATEWAY_SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    mount_gateway(mcp, gateway_session, thread_manager, kick)
    mount_history(mcp, gateway_session, thread_manager)
    return mcp
