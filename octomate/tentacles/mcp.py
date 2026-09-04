"""A tentacle serving its own MCP server beside the gateway.

A component, not a category: a tentacle composes it in beside being a channel or
an agent, and the host serves every server it finds this way at `/<name>/mcp`
behind the deployment's known bearers. The server is built by the class, not the
instance — one per tentacle type, however many are connected — and its tools
resolve the concrete tentacle, and whom a call acts as, from the session the call
names. That is what keeps a tool list constant for the prompt cache while the
tentacle a call lands on, and the credential it spends, vary per call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from octomate.managers.gateway import GatewaySession


class McpTentacle(ABC):
    """What a tentacle that serves MCP owes the host."""

    @classmethod
    @abstractmethod
    def mcp(cls, resolve_session: Callable[[], Awaitable[GatewaySession]]) -> FastMCP:
        """This tentacle type's server, every call of it resolved through
        `resolve_session` — the per-request lookup the host serves with, of the
        session a call names in its headers."""
