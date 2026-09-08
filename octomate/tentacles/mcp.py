"""A tentacle whose provider's MCP tools Octomate proxies on its one server.

A component, not a category: a tentacle composes it in beside being a channel
or an agent, or is nothing but it — a vendor's server, or a person's Linear or
GitHub, under `mcp:` — and the host adds every provider it finds this way to
the server at `/octomate/mcp` through namespace discovery and call helpers.
The provider's own server is never handed to a runtime directly: credentials
stay in Octomate. Only an explicit discovery or call resolves the turn, takes
from `auth` the credential it may spend, and reaches that provider. Discovery
returns the provider's own schemas, cached per credential; missing credentials
refuse the request with the account-linking instructions.

Two credentials, two subclasses. `OAuthMcpTentacle` acts as the person who
drove the turn, with the token they linked under this tentacle's id — from any
channel, since the person is the same everywhere — and is what the server's
`oauth` family links. `BareMcpTentacle` speaks with one operator credential for
every caller: the deployment's identity, not the person's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from functools import cached_property
from typing import TYPE_CHECKING

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.providers import Provider
from fastmcp.server.providers.proxy import ProxyTool
from fastmcp.server.transforms import Namespace
from fastmcp.utilities.versions import VersionSpec
from mcp.shared._httpx_utils import McpHttpClientFactory
from mcp.types import Tool
from pydantic import SecretStr

from octomate.config.mcp import (
    BareMcpConfig,
    GitHubMcpConfig,
    LinearMcpConfig,
    McpConfigVariant,
)
from octomate.managers.gateway import OctomateSession
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.oauth.base import McpConnectionAuth
from octomate.tentacles.base import Tentacle

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

# Upstreams can change their schemas without rotating a caller's credential.
TOOL_CATALOG_TTL = 3600.0


class PerCallerProxy(Provider):
    """The upstream's tools as the caller has them. Listings reuse the tentacle's
    catalog for the caller's current credential; a call is forwarded by name with
    no schema of Octomate's, since the upstream validates it itself.
    A caller with no credential is refused by `auth` during discovery or a call."""

    def __init__(
        self,
        upstream: Callable[[], Awaitable[Client]],
        list_tools: Callable[[], Awaitable[list[Tool]]],
    ) -> None:
        super().__init__()
        self.upstream = upstream
        self.list_upstream_tools = list_tools

    async def _list_tools(self) -> list[ProxyTool]:
        listed = await self.list_upstream_tools()
        return [ProxyTool.from_mcp_tool(self.upstream, tool) for tool in listed]

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> ProxyTool:
        return ProxyTool(client_factory=self.upstream, name=name, parameters={})


class McpTentacle(Tentacle, ABC):
    """What a tentacle that proxies a provider's MCP server declares, and gets.

    The instance declares the provider: the upstream URL, the label a card shows,
    the instructions the server carries for its tools — worded once however many
    tentacles share them — and the prefix its tools are listed under. What the
    concrete tentacle decides is `auth`: the credential a session's call speaks
    to the upstream with.
    """

    label: str
    upstream: str
    instructions: str
    # The prefix the served names carry (`linear_list_issues`), or None for a
    # provider that prefixes its own tools, as Slack does.
    prefix: str | None

    @cached_property
    def tool_catalogs(self) -> dict[SecretStr, tuple[float, list[Tool]]]:
        """Raw schemas only: proxy handlers keep the current turn's identity."""
        return {}

    @cached_property
    def tool_catalog_lock(self) -> asyncio.Lock:
        return asyncio.Lock()

    @property
    def serving(self) -> bool:
        """Whether this tentacle's tools are served at all; a channel may say no."""
        return True

    @abstractmethod
    async def auth(self, session: OctomateSession) -> McpConnectionAuth:
        """The credential a call from `session` speaks to the upstream with — or a
        `ToolError` saying why the session has none."""

    async def list_tools(
        self,
        auth: McpConnectionAuth,
        httpx_client_factory: McpHttpClientFactory | None,
    ) -> list[Tool]:
        """Reuse a credential's catalog across runtime mounts and MCP requests."""
        cached = self.tool_catalogs.get(auth.access_token)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        async with self.tool_catalog_lock:
            now = time.monotonic()
            for token, (expires_at, _) in tuple(self.tool_catalogs.items()):
                if expires_at <= now:
                    del self.tool_catalogs[token]
            cached = self.tool_catalogs.get(auth.access_token)
            if cached is not None:
                return cached[1]
            async with Client(
                StreamableHttpTransport(
                    self.upstream, auth=auth, httpx_client_factory=httpx_client_factory
                )
            ) as client:
                listed = await client.list_tools()
            self.tool_catalogs[auth.access_token] = (
                time.monotonic() + TOOL_CATALOG_TTL,
                listed,
            )
            return listed

    def provider(
        self,
        resolve_session: Callable[[], Awaitable[OctomateSession]],
        *,
        httpx_client_factory: McpHttpClientFactory | None = None,
    ) -> Provider:
        """This tentacle's proxy, every request to it resolved through
        `resolve_session` — the per-request lookup the host serves with, of the
        session a request names in its headers, or the one turn a server mounted
        in-process closes over. `httpx_client_factory` is how a test stands in
        for the provider's endpoint; the proxy itself only ever speaks to
        `upstream`."""

        async def upstream() -> Client:
            """The provider's server, spoken to as the caller: the client a
            request opens for its call, once resolved to a turn."""
            auth = await self.auth(await resolve_session())
            return Client(
                StreamableHttpTransport(
                    self.upstream, auth=auth, httpx_client_factory=httpx_client_factory
                )
            )

        async def list_tools() -> list[Tool]:
            auth = await self.auth(await resolve_session())
            return await self.list_tools(auth, httpx_client_factory)

        proxy = PerCallerProxy(upstream, list_tools)
        if self.prefix is None:
            return proxy
        return proxy.wrap_transform(Namespace(self.prefix))


class OAuthMcpTentacle(McpTentacle):
    """A provider whose tools act as the person who drove the turn, with the token
    they linked under this tentacle's id — the connector the concrete tentacle
    registers there. The server's `oauth` family is how the link happens."""

    async def auth(self, session: OctomateSession) -> McpConnectionAuth:
        profile = session.user_profile
        if profile is None:
            raise ToolError(
                f"{self.label}'s tools act as the person who drove this turn, and "
                "nobody registered did."
            )
        oauth = self.octomate.oauth
        access_token = await oauth.access_token(profile, self.id)
        if access_token is None:
            raise ToolError(
                f"This user has not linked their {self.label} account `{self.id}`. "
                f"Call `{CONNECT_TOOL}` with `{self.id}` to send them the "
                "authorization link — it goes to their direct messages — and "
                f"`{CONFIRM_TOOL}` to check it went through."
            )
        return McpConnectionAuth(
            access_token, lambda: oauth.invalidate(profile, self.id)
        )


class BareMcpTentacle(McpTentacle):
    """A vendor's MCP server under `mcp:`, spoken to with one operator credential
    for every caller. Its tools carry the configured key as their prefix unless
    the config names another, and it has no instructions of its own: the tools'
    descriptions are all a runtime reads."""

    def __init__(self, id: str, octomate: Octomate, *, config: BareMcpConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.label = id
        self.upstream = config.url
        self.instructions = ""
        self.prefix = config.prefix or id
        self.token = config.token

    async def auth(self, session: OctomateSession) -> McpConnectionAuth:
        return McpConnectionAuth(self.token, self.unauthorized)

    async def unauthorized(self) -> None:
        # An operator credential is the deployment's to fix, so the refusal is
        # logged once rather than retired the way a person's token is.
        logger.warning(
            "%s refused the operator credential of %s", self.upstream, self.id
        )


def build_mcp(id: str, config: McpConfigVariant, octomate: Octomate) -> McpTentacle:
    """Compose one configured MCP tentacle from its `type`, mirroring `build_channel`.

    The configured key is the tentacle id throughout — the prefix its tools carry
    and, for a linked account, the connector its tokens live under — which is what
    lets one vendor be mounted more than once, a key per account.

    The linked-account tentacles are imported here because their modules subclass
    `OAuthMcpTentacle` from this one: at the top, the import would be a cycle.
    """
    match config:
        case BareMcpConfig():
            return BareMcpTentacle(id, octomate, config=config)
        case GitHubMcpConfig():
            from octomate.tentacles.github import GitHubTentacle

            return GitHubTentacle(id, octomate, config=config)
        case LinearMcpConfig():
            from octomate.tentacles.linear import LinearTentacle

            return LinearTentacle(id, octomate, config=config)
