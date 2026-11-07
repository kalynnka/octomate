from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from pydantic import AnyUrl
from pydantic_ai import AgentCapability
from pydantic_ai.mcp import MCPToolset, ProcessToolCallback
from starlette.types import Receive, Scope, Send

from octomate import Octomate
from octomate.config.mcp import OAuthMcpConfig
from octomate.config.mcp.base import DeviceFlowConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager
from octomate.managers.oauth import OAuthConnector
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.mcp.server import LIST_MCP_TOOLS, McpToolCatalog, octomate_mcp
from octomate.schemas.mcp import (
    Mcp,
    McpInstallRequest,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.inkling import base as inkling_base
from octomate.tentacles.inkling.mcp import OctomateTools
from octomate.tentacles.mcp import McpTentacle
from octomate.types.json import JsonObject
from tests.support.managers import (
    FakeConversationManager,
    FakeThreadManager,
    fixed_session,
)


@asynccontextmanager
async def served_inkling_mcp(
    host: Octomate, monkeypatch: pytest.MonkeyPatch, *, lifespan: bool = True
) -> AsyncGenerator[None]:
    """Exercise Inkling's HTTP bearer and session headers against the real MCP app."""

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        # FastMCP's runner omits the state needed by Octomate's lifespan result.
        await host({**scope, "state": {}}, receive, send)

    def asgi(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=host),
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    async with run_asgi_lifespan(lifespan_app) if lifespan else nullcontext():

        class ServedMcp(MCPToolset[None]):
            def __init__(
                self,
                client: str,
                *,
                id: str,
                headers: dict[str, str],
                process_tool_call: ProcessToolCallback,
                max_retries: int,
            ) -> None:
                super().__init__(
                    Client(
                        StreamableHttpTransport(
                            client, headers=headers, httpx_client_factory=asgi
                        )
                    ),
                    id=id,
                    process_tool_call=process_tool_call,
                    max_retries=max_retries,
                )

        with monkeypatch.context() as patch:
            patch.setattr(inkling_base, "MCPToolset", ServedMcp)
            yield


async def discover(server: Client | FastMCP, namespace: str) -> McpToolCatalog:
    result = await server.call_tool(LIST_MCP_TOOLS, {"namespace": namespace})
    return McpToolCatalog.model_validate(result.structured_content)


def configured_mcp(*, client_id: str = "test-app") -> OAuthMcpConfig:
    return OAuthMcpConfig(
        url=AnyUrl("https://mcp.example/mcp"),
        client_id=client_id,
        scopes=["tools:read"],
        flows=[
            DeviceFlowConfig(
                device_authorization_endpoint=AnyUrl("https://auth.example/device"),
                token_endpoint=AnyUrl("https://auth.example/token"),
            )
        ],
    )


async def install_tentacle(tentacle: McpTentacle, profile: UserProfile) -> Mcp:
    host = tentacle.octomate
    host.mcp.oauth = host.oauth
    host.mcp.cipher = host.oauth.cipher
    host.mcp.users = host.oauth.users
    connector = host.oauth.connectors.get(tentacle.id)
    if connector is not None:
        host.oauth.connectors[tentacle.id] = OAuthConnector(
            id=connector.id,
            flows=connector.flows,
            callback_transport=connector.callback_transport,
            mcp_url=AnyUrl(tentacle.upstream),
        )
    host.mcp.tentacles[tentacle.id] = tentacle
    owner = await host.users.owner(profile)
    assert owner is not None
    for installed in await host.mcp.list(owner.id):
        if installed.tentacle_id == tentacle.id:
            return installed
    return await host.mcp.install(
        owner.id,
        McpInstallRequest(
            name=tentacle.label,
            namespace=tentacle.id,
            url=AnyUrl(tentacle.upstream),
            tentacle_id=tentacle.id,
        ),
    )


def core_tool_capabilities(
    session: OctomateSession,
    *,
    threads: ThreadManager | None = None,
    conversations: ConversationManager | None = None,
) -> list[AgentCapability[None]]:
    """Use the served tool definitions in model tests without an HTTP listener."""
    server = core_tool_server(session, threads=threads, conversations=conversations)
    return OctomateTools.capabilities(
        MCPToolset(Client(server), process_tool_call=OctomateTools.call, max_retries=2)
    )


async def gateway_schemas(session: OctomateSession) -> dict[str, JsonObject]:
    server = core_tool_server(session)
    return {
        tool.name.removeprefix("gateway_"): tool.parameters
        for tool in await server.list_tools()
        if tool.name.startswith("gateway_")
    }


def core_tool_server(
    session: OctomateSession,
    *,
    threads: ThreadManager | None = None,
    conversations: ConversationManager | None = None,
) -> FastMCP:
    return octomate_mcp(
        fixed_session(session),
        threads or FakeThreadManager(),
        manager=McpManager(users=UserManager(), cipher=None),
        conversations=conversations or FakeConversationManager(),
    )
