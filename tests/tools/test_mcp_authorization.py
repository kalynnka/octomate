"""Native OAuth grants authorize the mounted MCP transport and its user-owned tools."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx2
import pytest
from pydantic import AnyHttpUrl, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.config.base import OctomateConfig
from octomate.config.channels import ChannelConfig
from octomate.database import async_session
from octomate.mcp.gateway import CLIENT_HEADER
from octomate.mcp.server import OCTOMATE_MCP_PATH
from octomate.schemas.authorization import OAuthGrant, OAuthToken
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import UserProfile
from tests.managers.test_authorization_routes import (
    ORIGIN,
    RESOURCE,
    approve,
    exchange,
    register,
    sign_in,
    start,
)
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user, auth_config
from tests.tools.test_mcp_serving import LIST_TOOLS, over, served


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def host() -> Octomate:
    config = auth_config()
    config.oauth_issuer_url = AnyHttpUrl(ORIGIN)
    host = Octomate(config=OctomateConfig(auth=config))
    host.connect(FakeAgent(id="claude"))
    host.connect(FakeAgent(id="other"))
    host.connect(
        FakeChannelTentacle(
            octomate=host, config=ChannelConfig(type="fake", agents=["other"])
        )
    )
    return host


@pytest.fixture
async def browser(host: Octomate) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=host), base_url=ORIGIN
    ) as browser:
        yield browser


@pytest.mark.parametrize("credential", [None, "unknown"])
async def test_mcp_challenge_discovers_root_metadata(
    browser: httpx2.AsyncClient, credential: str | None
) -> None:
    response = await browser.post(
        OCTOMATE_MCP_PATH,
        json=LIST_TOOLS,
        headers={
            "Host": "attacker.test",
            **({"Authorization": f"Bearer {credential}"} if credential else {}),
        },
    )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    metadata_url = f"{ORIGIN}/.well-known/oauth-protected-resource{OCTOMATE_MCP_PATH}"
    assert f'resource_metadata="{metadata_url}"' in challenge
    assert 'scope="mcp"' in challenge
    assert ('error="invalid_token"' in challenge) == (credential is not None)
    metadata = await browser.get(metadata_url)
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == RESOURCE
    assert metadata.json()["authorization_servers"] == [f"{ORIGIN}/"]
    issuer = await browser.get(f"{ORIGIN}/.well-known/oauth-authorization-server")
    assert issuer.json()["authorization_endpoint"] == f"{ORIGIN}/oauth/authorize"


async def test_oauth_consent_opens_owners_tools_and_revocation_closes_them(
    host: Octomate, browser: httpx2.AsyncClient
) -> None:
    assert host.auth is not None
    client_id = await register(browser)
    user_id = await sign_in(browser, host.auth)
    ticket = await start(browser, client_id)
    code = await approve(browser, ticket, user_id)
    response = await exchange(browser, client_id, code)
    assert response.status_code == 200
    tokens = response.json()
    bearer = tokens["access_token"]
    # A client id that also names a user must never select that user's accounts.
    await a_user(client_id, profiles={"im": "someone-else"})
    async with async_session() as session:
        session.add(
            UserProfile(
                user_id=UUID(user_id),
                channel_tentacle_id="im",
                channel_user_id="alice-in-chat",
            )
        )
        await session.commit()
    principal = await host.bearers.verify_token(bearer)
    assert principal is not None
    assert principal.client_id == client_id
    assert principal.subject == "alice"
    assert principal.resource == RESOURCE
    assert principal.expires_at is not None
    assert principal.claims["iss"] == f"{ORIGIN}/"
    # MCP consent cannot authorize the separate hooks or ACP resource.
    assert await host.bearers.owner(bearer, scope="hooks") is None
    assert await host.bearers.owner(bearer, scope="acp") is None

    async with served(host):
        async with over(
            host, host, {"Authorization": f"Bearer {bearer}", CLIENT_HEADER: "claude"}
        ) as client:
            result = await client.call_tool(
                "gateway_inspect", {"reveal": "destinations"}
            )
            assert TypeAdapter(list[ChannelAddress]).validate_json(result.data) == [
                ChannelAddress("im", "dm", "", "alice-in-chat")
            ]
            refreshed = await browser.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": tokens["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            assert refreshed.status_code == 200
            rotated = refreshed.json()
            async with over(
                host,
                host,
                {
                    "Authorization": f"Bearer {rotated['access_token']}",
                    CLIENT_HEADER: "claude",
                },
            ) as refreshed_client:
                refreshed_result = await refreshed_client.call_tool(
                    "gateway_inspect", {"reveal": "destinations"}
                )
                assert refreshed_result.data == result.data
            revoked = await browser.post(
                "/oauth/revoke",
                data={"client_id": client_id, "token": rotated["refresh_token"]},
            )
            assert revoked.status_code == 200
            for access in (bearer, rotated["access_token"]):
                denied = await browser.post(
                    OCTOMATE_MCP_PATH,
                    json=LIST_TOOLS,
                    headers={"Authorization": f"Bearer {access}"},
                )
                assert denied.status_code == 401

        runtime = await host.auth.runtime_api_key(
            UUID(user_id), agent_id="claude", scopes=["mcp"]
        )
        assert runtime.bearer is not None
        async with over(
            host,
            host,
            {"Authorization": f"Bearer {runtime.bearer.get_secret_value()}"},
        ) as client:
            assert await client.list_tools()
    assert host.gateway.sessions == {}


@pytest.mark.parametrize(
    "invalidity",
    [
        "access-expired",
        "grant-expired",
        "wrong-resource",
        "wrong-scope",
        "token-scope",
        "disabled",
        "refresh",
    ],
)
async def test_mcp_rechecks_token_and_grant_on_each_request(
    host: Octomate,
    browser: httpx2.AsyncClient,
    invalidity: Literal[
        "access-expired",
        "grant-expired",
        "wrong-resource",
        "wrong-scope",
        "token-scope",
        "disabled",
        "refresh",
    ],
) -> None:
    assert host.auth is not None
    client_id = await register(browser)
    user_id = await sign_in(browser, host.auth)
    code = await approve(browser, await start(browser, client_id), user_id)
    tokens = (await exchange(browser, client_id, code)).json()
    bearer = tokens["access_token"]
    async with served(host):
        async with over(host, host, {"Authorization": f"Bearer {bearer}"}) as client:
            assert await client.list_tools()
        async with async_session() as session:
            [token] = await session.list(OAuthToken)
            grant = await session.get(OAuthGrant, token.grant_id)
            assert grant is not None
            expired = datetime.now(UTC) - timedelta(seconds=1)
            if invalidity == "access-expired":
                token.access_expires_at = expired
            elif invalidity == "grant-expired":
                grant.expires_at = expired
            elif invalidity == "wrong-resource":
                host.auth.config.oauth_issuer_url = AnyHttpUrl("https://other.test")
            elif invalidity == "wrong-scope":
                grant.scopes = ["hooks"]
            elif invalidity == "token-scope":
                token.scopes = ["hooks"]
            elif invalidity == "disabled":
                host.auth.config.oauth_issuer_url = None
            else:
                bearer = tokens["refresh_token"]
            await session.commit()
        response = await browser.post(
            OCTOMATE_MCP_PATH,
            json=LIST_TOOLS,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert response.status_code == 401
