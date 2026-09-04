"""A tentacle serving a provider's MCP server beside the gateway, as the person
who drove the turn.

A component, not a category: a tentacle composes it in beside being a channel
or an agent, and the host serves every server it finds this way at
`/<name>/mcp` behind the deployment's known bearers. The provider's own server
is never handed to a runtime directly, because the providers worth serving take
a user token and nothing else — every call acts as the human who authorized it
— and that token must not leave Octomate. So the server here has no tools of
its own: a request resolves the turn it belongs to, the tentacle it acts
through and the person it acts as, takes that person's token from the OAuth
manager under the tentacle's own connector, and lists or calls the provider
with that bearer. A runtime is listed what the provider lists that person,
worded as the provider words it for them — as constant as the provider keeps
it, which is what the prompt cache needs — and a person who has not linked
their account yet is listed nothing.

The server is built by the class, not the instance — one per tentacle type,
however many are connected — while the instance a request lands on, and the
credential it spends, vary per request. Two tools are Octomate's own:
`connect_<name>` sends the person the authorization link as a card in their
direct messages, and `confirm_<name>` reports whether it went through; a call
refused for want of a token names them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import ClassVar, Self

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.providers import Provider
from fastmcp.server.providers.proxy import ProxyTool
from fastmcp.utilities.versions import VersionSpec
from mcp.shared._httpx_utils import McpHttpClientFactory

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.managers.gateway import GatewaySession
from octomate.oauth.base import McpConnectionAuth
from octomate.schemas.oauth import AuthorizationLink
from octomate.schemas.user import UserProfile
from octomate.tentacles.base import Tentacle

# The linking contract every provider shares, appended to the provider's own
# instructions under the tool names the server actually registers.
LINKING_INSTRUCTION = """\

{label}'s tools are listed here only to a person who has linked their {label}
account once, and act as that person. When none are listed, or a call is refused
for that, call `{connect}`, then tell the person the link is in their direct
messages — you are not given it and cannot repeat or rebuild it. Opening the
link and approving is the whole of it; `{confirm}` says whether it went through,
and the tools are listed from their next turn on.
"""


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

    The class declares the provider: the server's name, the upstream URL, the
    label a card shows, and the instructions the runtime reads. The instance
    registers, under its own id, the connector the person's tokens live under.
    What the concrete tentacle decides is `onbehalf`: given the session a call
    names, which instance the call acts through and which person it acts as.
    """

    server_name: ClassVar[str]
    label: ClassVar[str]
    upstream: ClassVar[str]
    instructions: ClassVar[str]

    @classmethod
    @abstractmethod
    def onbehalf(cls, session: GatewaySession) -> tuple[Self, UserProfile]:
        """The instance a call acts through and the person it acts as, both the
        turn's own — or a `ToolError` saying why this turn has neither."""

    @classmethod
    def connect_tool(cls) -> str:
        """The tool that sends a person the link authorizing their account."""
        return f"connect_{cls.server_name}"

    @classmethod
    def confirm_tool(cls) -> str:
        """The tool that reports whether that authorization went through."""
        return f"confirm_{cls.server_name}"

    @classmethod
    def mcp(
        cls,
        resolve_session: Callable[[], Awaitable[GatewaySession]],
        *,
        httpx_client_factory: McpHttpClientFactory | None = None,
    ) -> FastMCP:
        """This tentacle type's server, every request to it resolved through
        `resolve_session` — the per-request lookup the host serves with, of the
        session a request names in its headers. `httpx_client_factory` is how a
        test stands in for the provider's endpoint; the server itself only ever
        speaks to `upstream`."""
        connect, confirm = cls.connect_tool(), cls.confirm_tool()

        async def upstream() -> Client:
            """The provider's server, spoken to as the caller: the client a
            request opens for its one listing or call, once resolved to a turn."""
            tentacle, profile = cls.onbehalf(await resolve_session())
            oauth = tentacle.octomate.oauth
            access_token = await oauth.access_token(profile, tentacle.id)
            if access_token is None:
                raise ToolError(
                    f"This user has not linked their {cls.label} account on "
                    f"{tentacle.id}. Call `{connect}` to send them the authorization "
                    f"link — it goes to their direct messages — and `{confirm}` to "
                    "check it went through."
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

        server = FastMCP(
            cls.server_name,
            instructions=cls.instructions
            + LINKING_INSTRUCTION.format(
                label=cls.label, connect=connect, confirm=confirm
            ),
            providers=[PerCallerProxy(upstream)],
        )
        gateway_session = Depends(resolve_session)

        @server.tool(
            name=connect,
            description=(
                f"Send this user a link that authorizes their own {cls.label} "
                "account. The link goes to their direct messages, never to the "
                "conversation, and is not returned here."
            ),
        )
        async def send_link(session: GatewaySession = gateway_session) -> str:
            tentacle, profile = cls.onbehalf(session)
            address = session.conversation_address
            channel = (
                session.channels.get(address.channel_tentacle_id)
                if address is not None
                else None
            )
            if address is None or channel is None:
                raise ToolError(
                    "The link goes to the person's direct messages on the channel "
                    "this turn is on, and this call has no turn on a channel."
                )
            authorization = await tentacle.octomate.oauth.start(profile, tentacle.id)
            if not isinstance(authorization, AuthorizationLink):
                raise TypeError(
                    f"{tentacle.id} is not on an authorization-code connector"
                )
            # The link goes to the channel as an authorization of its own, for the
            # channel to present — never through this return value, which the
            # model reads and could repeat into a reply.
            await channel.feelers.oauth.present(
                address,
                OAuthAuthorizationEvent(
                    connector_id=tentacle.id,
                    label=cls.label,
                    authorization_uri=str(authorization.authorization_uri),
                ),
            )
            return (
                "The authorization link is on its way to this user's direct messages."
            )

        @server.tool(
            name=confirm,
            description=(
                f"Report whether this user's {cls.label} connection has finished."
            ),
        )
        async def report(session: GatewaySession = gateway_session) -> str:
            tentacle, profile = cls.onbehalf(session)
            status = await tentacle.octomate.oauth.connection_status(
                profile, tentacle.id
            )
            if status == "active":
                return f"{cls.label} is connected: its tools now act as this user here."
            if status == "invalid":
                return (
                    f"{cls.label} was connected and is not any more — the "
                    "authorization was revoked or expired. Offer to send a fresh "
                    f"link with `{connect}`."
                )
            return (
                f"{cls.label} is not connected yet. The link finishes the "
                "connection by itself once they approve it; there is nothing to do "
                "here but wait and check again."
            )

        return server
