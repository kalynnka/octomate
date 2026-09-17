from __future__ import annotations

import uuid

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.mcp import OAuthMcpConfig
from octomate.config.mcp.base import AuthorizationCodeFlowConfig, DeviceFlowConfig
from octomate.config.oauth import OAuthConfig
from octomate.database import async_session
from octomate.mcp.server import (
    DISABLE_MCP,
    ENABLE_MCP,
    INSTALL_MCP,
    LIST_MCP_TENTACLES,
    LIST_MCPS,
    UNINSTALL_MCP,
    tentacles_mcp,
)
from octomate.schemas.mcp import McpServerSummary
from octomate.schemas.oauth import OAuthConnection
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.mcp import BareMcpTentacle, build_mcp
from octomate.types.json import JsonObject
from tests.agent.test_mcp import ENCRYPTION_KEY, a_turn
from tests.support.managers import fixed_session
from tests.support.users import a_user


@pytest.fixture
async def deployment(in_memory_engine: AsyncEngine) -> tuple[Octomate, User, FastMCP]:
    host = Octomate(
        config=OctomateConfig(
            oauth=OAuthConfig(callback_base_uri=AnyHttpUrl("http://localhost:8000"))
        ),
        oauth_encryption_key=ENCRYPTION_KEY,
    )

    def unexpected_request(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("MCP management must not contact an OAuth provider")

    host.oauth.httpx_client_factory = lambda headers=None, timeout=None, auth=None: (
        httpx2.AsyncClient(transport=httpx2.MockTransport(unexpected_request))
    )
    user = await a_user("alice")
    server = tentacles_mcp(
        fixed_session(a_turn(UserProfile(user_id=user.id))), manager=host.mcp
    )
    return host, user, server


async def test_catalog_and_multiple_installs_keep_secrets_and_owners_separate(
    deployment: tuple[Octomate, User, FastMCP],
) -> None:
    host, alice, server = deployment
    host.connect(
        BareMcpTentacle(
            "research",
            host,
            url="https://mcp.example/mcp",
            token=SecretStr("operator-secret"),
        )
    )
    catalog = await server.call_tool(LIST_MCP_TENTACLES, {})
    assert "research" in str(catalog)
    assert "operator-secret" not in str(catalog)
    assert await host.mcp.list(alice.id) == []

    installed = []
    for namespace in ("research_work", "research_personal"):
        result = await server.call_tool(
            INSTALL_MCP,
            {
                "name": namespace,
                "namespace": namespace,
                "url": "https://mcp.example/mcp",
                "tentacle_id": "research",
            },
        )
        summary = McpServerSummary.model_validate(result.structured_content)
        assert summary.namespace == f"personal/{namespace}"
        assert summary.auth_kind == "bearer"
        assert summary.enabled
        assert summary.oauth is None
        assert "operator-secret" not in str(result)
        installed.append(summary.id)
    assert len(set(installed)) == 2
    with pytest.raises(ToolError, match="already installed"):
        await server.call_tool(
            INSTALL_MCP,
            {
                "name": "Duplicate",
                "namespace": "research_work",
                "url": "https://mcp.example/mcp",
            },
        )
    bob = await a_user("bob")
    other = tentacles_mcp(
        fixed_session(a_turn(UserProfile(user_id=bob.id))), manager=host.mcp
    )
    assert (await other.call_tool(LIST_MCPS, {})).structured_content == {"result": []}
    await other.call_tool(
        INSTALL_MCP,
        {
            "name": "Bob's research",
            "namespace": "research_work",
            "url": "https://mcp.example/mcp",
        },
    )
    assert len(await host.mcp.list(alice.id)) == 2
    assert len(await host.mcp.list(bob.id)) == 1


async def test_oauth_discovery_lists_both_flows_and_preserves_grants_until_uninstall(
    deployment: tuple[Octomate, User, FastMCP],
) -> None:
    host, user, server = deployment
    host.connect(
        build_mcp(
            "coding",
            OAuthMcpConfig(
                url=AnyHttpUrl("https://mcp.example/mcp"),
                client_id="app-id",
                client_secret=SecretStr("app-secret"),
                flows=[
                    DeviceFlowConfig(
                        device_authorization_endpoint=AnyHttpUrl(
                            "https://auth.example/device"
                        ),
                        token_endpoint=AnyHttpUrl("https://auth.example/token"),
                    ),
                    AuthorizationCodeFlowConfig(
                        authorization_endpoint=AnyHttpUrl(
                            "https://auth.example/authorize"
                        ),
                        token_endpoint=AnyHttpUrl("https://auth.example/token"),
                        token_endpoint_auth_method="client_secret_post",
                    ),
                ],
            ),
            host,
        )
    )
    result = await server.call_tool(
        INSTALL_MCP,
        {
            "name": "Coding",
            "namespace": "coding",
            "url": "https://mcp.example/mcp",
            "tentacle_id": "coding",
        },
    )
    summary = McpServerSummary.model_validate(result.structured_content)
    assert summary.oauth is not None
    assert summary.oauth.status is None
    assert summary.oauth.flows == ["device", "authorization_code"]
    assert "app-secret" not in str(result)
    grant = OAuthConnection(
        user_id=user.id,
        connector_id="coding",
        mcp_id=summary.id,
        encrypted_tokens=b"encrypted-token",
    )
    async with async_session() as session:
        session.add(grant)
        await session.commit()
    for tool, enabled in ((DISABLE_MCP, False), (ENABLE_MCP, True)):
        result = await server.call_tool(tool, {"mcp_id": str(summary.id)})
        updated = McpServerSummary.model_validate(result.structured_content)
        assert updated.enabled is enabled
        assert updated.oauth is not None
        assert updated.oauth.status == "active"
        assert "encrypted-token" not in str(result)
    async with async_session() as session:
        saved = await session.get(OAuthConnection, grant.id)
        assert saved is not None
        saved.status = "invalid"
        await session.commit()
    result = await server.call_tool(LIST_MCPS, {})
    assert result.structured_content is not None
    [listed] = TypeAdapter(list[McpServerSummary]).validate_python(
        result.structured_content["result"]
    )
    assert listed.oauth is not None
    assert listed.oauth.status == "invalid"
    await server.call_tool(UNINSTALL_MCP, {"mcp_id": str(summary.id)})
    async with async_session() as session:
        assert await session.get(OAuthConnection, grant.id) is None


@pytest.mark.parametrize("auth", ["none", "oauth"])
async def test_remote_install_needs_no_tentacle_or_provider_request(
    deployment: tuple[Octomate, User, FastMCP],
    auth: str,
) -> None:
    host, user, server = deployment
    result = await server.call_tool(
        INSTALL_MCP,
        {
            "name": "Remote",
            "namespace": "remote",
            "url": "https://mcp.example/mcp",
            "auth": auth,
        },
    )
    summary = McpServerSummary.model_validate(result.structured_content)
    assert summary.auth_kind == auth
    if auth == "oauth":
        assert summary.oauth is not None
        assert summary.oauth.status is None
        assert summary.oauth.flows == ["authorization_code"]
    else:
        assert summary.oauth is None
    assert (await host.mcp.list(user.id))[0].tentacle_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"auth": "bearer"},
        {"url": "http://mcp.example/mcp"},
        {"url": "https://mcp.example/mcp?token=secret"},
        {"namespace": "personal/remote"},
        {"tentacle_id": "missing"},
        {"tentacle_id": "research", "auth": "none"},
        {"tentacle_id": "research", "url": "https://different.example/mcp"},
    ],
)
async def test_install_rejects_invalid_auth_endpoints_and_tentacle_overrides(
    deployment: tuple[Octomate, User, FastMCP],
    overrides: JsonObject,
) -> None:
    host, user, server = deployment
    host.connect(BareMcpTentacle("research", host, url="https://mcp.example/mcp"))
    with pytest.raises((ToolError, ValidationError)):
        await server.call_tool(
            INSTALL_MCP,
            {"name": "Remote", "namespace": "remote", "url": "https://mcp.example/mcp"}
            | overrides,
        )
    assert await host.mcp.list(user.id) == []


@pytest.mark.parametrize(
    "profile", [None, UserProfile(), UserProfile(user_id=uuid.uuid4())]
)
async def test_management_requires_a_registered_owner(
    deployment: tuple[Octomate, User, FastMCP],
    profile: UserProfile | None,
) -> None:
    host, user, _ = deployment
    server = tentacles_mcp(fixed_session(a_turn(profile)), manager=host.mcp)
    calls: list[tuple[str, JsonObject]] = [
        (LIST_MCP_TENTACLES, {}),
        (
            INSTALL_MCP,
            {"name": "Remote", "namespace": "remote", "url": "https://mcp.example/mcp"},
        ),
        *[
            (tool, {"mcp_id": str(uuid.uuid4())})
            for tool in (ENABLE_MCP, DISABLE_MCP, UNINSTALL_MCP)
        ],
    ]
    for tool, arguments in calls:
        with pytest.raises(ToolError):
            await server.call_tool(tool, arguments)
    assert await host.mcp.list(user.id) == []


async def test_injected_owner_is_not_a_tool_argument(
    deployment: tuple[Octomate, User, FastMCP],
) -> None:
    host, alice, server = deployment
    bob = await a_user("bob")
    for name in (
        LIST_MCP_TENTACLES,
        INSTALL_MCP,
        ENABLE_MCP,
        DISABLE_MCP,
        UNINSTALL_MCP,
    ):
        tool = await server.get_tool(name)
        assert tool is not None
        assert "user" not in tool.parameters["properties"]
    with pytest.raises((ToolError, ValidationError)):
        await server.call_tool(
            INSTALL_MCP,
            {
                "user": {"id": str(bob.id), "username": bob.username},
                "name": "Remote",
                "namespace": "remote",
                "url": "https://mcp.example/mcp",
            },
        )
    assert await host.mcp.list(alice.id) == []
    assert await host.mcp.list(bob.id) == []
