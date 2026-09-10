from fastmcp import Client, FastMCP
from pydantic import AnyUrl

from octomate.config.mcp import OAuthMcpConfig
from octomate.config.mcp.base import DeviceFlowConfig
from octomate.managers.oauth import OAuthConnector
from octomate.mcp.server import LIST_MCP_TOOLS, McpToolCatalog
from octomate.schemas.mcp import (
    Mcp,
    McpInstallRequest,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.mcp import McpTentacle


async def discover(server: Client | FastMCP, namespace: str) -> McpToolCatalog:
    result = await server.call_tool(LIST_MCP_TOOLS, {"namespace": namespace})
    return McpToolCatalog.model_validate(result.structured_content)


def configured_mcp(*, client_id: str = "test-app") -> OAuthMcpConfig:
    return OAuthMcpConfig(
        url=AnyUrl("https://mcp.example/mcp"),
        client_id=client_id,
        scopes=["tools:read"],
        flow=DeviceFlowConfig(
            device_authorization_endpoint=AnyUrl("https://auth.example/device"),
            token_endpoint=AnyUrl("https://auth.example/token"),
        ),
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
            flow=connector.flow,
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
