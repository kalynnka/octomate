"""A tentacle whose provider's MCP tools Octomate proxies on its one server, as
the person who drove the turn.

A component, not a category: a tentacle composes it in beside being a channel
or an agent, and the host adds every provider it finds this way to the server
at `/octomate/mcp`. The provider's own server is never handed to a runtime
directly, because the providers worth serving take a user token and nothing
else — every call acts as the human who authorized it — and that token must
not leave Octomate. So the proxy has no tools of its own: a request resolves
the turn it belongs to, the tentacle it acts through and the person it acts
as, takes that person's token from the OAuth manager under the tentacle's own
connector, and lists or calls the provider with that bearer. A runtime is
listed what the provider lists that person, worded as the provider words it
for them — as constant as the provider keeps it, which is what the prompt
cache needs — and a person who has not linked their account yet is listed
nothing. The linking is the server's own `oauth` family, which knows a
provider by the tentacle's id — the connector its tokens live under.

The proxy is built by the class, not the instance — one per tentacle type,
however many are connected — while the instance a request lands on, and the
credential it spends, vary per request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import ClassVar, Self

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.providers import Provider
from fastmcp.server.providers.proxy import ProxyTool
from fastmcp.utilities.versions import VersionSpec
from mcp.shared._httpx_utils import McpHttpClientFactory

from octomate.managers.gateway import GatewaySession
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.oauth.base import McpConnectionAuth
from octomate.schemas.user import UserProfile
from octomate.tentacles.base import Tentacle


class PerCallerProxy(Provider):
    """The upstream's tools as the caller has them. A listing asks the upstream
    with the caller's own token, since what it lists — and how it words it — is
    theirs; a call is forwarded by name with no schema of Octomate's, since the
    upstream validates it itself. A caller with no standing here — no turn on
    the tentacle, no token yet — is listed nothing, and what a call of theirs
    gets is the reason, raised by `upstream`."""

    def __init__(self, upstream: Callable[[], Awaitable[Client]]) -> None:
        super().__init__()
        self.upstream = upstream

    async def _list_tools(self) -> list[ProxyTool]:
        try:
            client = await self.upstream()
        except ToolError:
            return []
        async with client:
            listed = await client.list_tools()
        return [ProxyTool.from_mcp_tool(self.upstream, tool) for tool in listed]

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> ProxyTool:
        return ProxyTool(client_factory=self.upstream, name=name, parameters={})


class McpTentacle(Tentacle, ABC):
    """What a tentacle that proxies a provider's MCP server declares, and gets.

    The class declares the provider's server: the upstream URL, the label a card
    shows, and the instructions the server carries for its tools. The instance
    is the provider the link tools know, by its id, and registers under that id
    the connector the person's tokens live under. What the concrete tentacle
    decides is `onbehalf`: given the session a call names, which instance the
    call acts through and which person it acts as.
    """

    label: ClassVar[str]
    upstream: ClassVar[str]
    instructions: ClassVar[str]

    @classmethod
    @abstractmethod
    def onbehalf(cls, session: GatewaySession) -> tuple[Self, UserProfile]:
        """The instance a call acts through and the person it acts as, both the
        turn's own — or a `ToolError` saying why this turn has neither."""

    @classmethod
    def provider(
        cls,
        resolve_session: Callable[[], Awaitable[GatewaySession]],
        *,
        httpx_client_factory: McpHttpClientFactory | None = None,
    ) -> PerCallerProxy:
        """This tentacle type's proxy, every request to it resolved through
        `resolve_session` — the per-request lookup the host serves with, of the
        session a request names in its headers, or the one turn a server mounted
        in-process closes over. `httpx_client_factory` is how a test stands in
        for the provider's endpoint; the proxy itself only ever speaks to
        `upstream`."""

        async def upstream() -> Client:
            """The provider's server, spoken to as the caller: the client a
            request opens for its one listing or call, once resolved to a turn."""
            tentacle, profile = cls.onbehalf(await resolve_session())
            oauth = tentacle.octomate.oauth
            access_token = await oauth.access_token(profile, tentacle.id)
            if access_token is None:
                raise ToolError(
                    f"This user has not linked their {cls.label} account on "
                    f"{tentacle.id}. Call `{CONNECT_TOOL}` with `{tentacle.id}` to "
                    "send them the authorization link — it goes to their direct "
                    f"messages — and `{CONFIRM_TOOL}` to check it went through."
                )
            return Client(
                StreamableHttpTransport(
                    cls.upstream,
                    auth=McpConnectionAuth(
                        access_token,
                        lambda: oauth.invalidate(profile, tentacle.id),
                    ),
                    httpx_client_factory=httpx_client_factory,
                )
            )

        return PerCallerProxy(upstream)
