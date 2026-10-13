"""The Lark channel's user OAuth, profile linking and authenticated MCP offering."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic import AnyHttpUrl, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import LarkChannelConfig, OctomateConfig
from octomate.config.channels import LarkOAuthClientConfig
from octomate.managers.mcp import McpUnavailable
from octomate.schemas.mcp import McpInstallRequest
from octomate.schemas.oauth import AuthorizationLink, OAuthGrant
from octomate.schemas.user import User
from octomate.tentacles.lark import LarkTentacle
from octomate.tentacles.lark.oauth import LarkMcpBearerAuth, LarkOAuthConnector
from tests.agent.test_mcp import a_turn, upstream_of
from tests.channels.slack.test_mcp import into
from tests.managers.test_oauth_routes import browser
from tests.support.users import a_user

KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())


@pytest.fixture
async def channel() -> AsyncIterator[LarkTentacle]:
    host = Octomate(oauth_encryption_key=KEY)
    host.users.authorization_base_uri = AnyHttpUrl("https://octomate.example")

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/open-apis/authen/v1/user_info":
            username = request.headers["Authorization"].removeprefix("Bearer u-")
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "open_id": f"ou_{username}",
                        "name": username,
                        "user_id": "provider-id-is-not-an-octomate-owner",
                    },
                },
            )
        assert (
            str(request.url) == "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
        )
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["cli-dev"]
        assert form["client_secret"] == ["app-secret"]
        assert "Authorization" not in request.headers
        if form["grant_type"] == ["authorization_code"]:
            assert form["redirect_uri"] == ["http://localhost:8000/oauth/lark/callback"]
            assert len(form["code_verifier"][0]) >= 43
            username = form["code"][0]
            return httpx2.Response(
                200,
                json={
                    "access_token": f"u-{username}",
                    "refresh_token": f"r-{username}",
                    "expires_in": 60,
                },
            )
        assert form["grant_type"] == ["refresh_token"]
        username = form["refresh_token"][0].removeprefix("r-")
        return httpx2.Response(
            200,
            json={
                "access_token": f"refreshed-{username}",
                "refresh_token": f"rotated-{username}",
                "expires_in": 3600,
            },
        )

    host.oauth.httpx_client_factory = into(httpx2.MockTransport(respond))
    tentacle = host.connect(
        LarkTentacle(
            "lark",
            host,
            config=LarkChannelConfig(
                app_id="cli-dev",
                app_secret=SecretStr("app-secret"),
                agents=["inkling"],
                mcp=True,
                oauth=LarkOAuthClientConfig(),
            ),
        )
    )
    try:
        yield tentacle
    finally:
        tentacle.ws_client._cache._cron.cancel()
        await asyncio.gather(tentacle.ws_client._cache._cron, return_exceptions=True)


def test_mcp_requires_oauth_settings() -> None:
    with pytest.raises(ValidationError, match="needs an `oauth` block"):
        LarkChannelConfig(
            app_id="cli-dev",
            app_secret=SecretStr("secret"),
            agents=["inkling"],
            mcp=True,
        )


def test_user_oauth_requires_token_encryption() -> None:
    with pytest.raises(ValidationError, match=r"oauth\.encryption_key is required"):
        OctomateConfig.model_validate(
            {
                "tentacles": {
                    "inkling": {
                        "type": "inkling",
                        "models": [{"name": "openai:gpt-4o"}],
                    },
                    "lark": {
                        "type": "lark",
                        "app_id": "cli-dev",
                        "app_secret": "secret",
                        "agents": ["inkling"],
                        "oauth": {},
                    },
                }
            }
        )


@pytest.mark.parametrize("enabled", [False, True])
async def test_channel_controls_its_mcp_offering(
    channel: LarkTentacle, enabled: bool
) -> None:
    channel.config.mcp = enabled
    assert [info.id for info in channel.octomate.mcp.available()] == (
        ["lark"] if enabled else []
    )
    assert list(channel.octomate.oauth.connectors) == ["lark"]


async def test_users_authorize_refresh_and_call_with_their_own_tokens(
    channel: LarkTentacle, in_memory_engine: AsyncEngine
) -> None:
    host = channel.octomate
    seen: list[dict[str, str]] = []
    upstream = FastMCP("feishu")

    @upstream.tool(name="get-user")
    async def get_user() -> str:
        headers = get_http_headers(include_all=True)
        seen.append(headers)
        return headers["x-lark-mcp-uat"]

    async with upstream_of(upstream) as transport, host.mcp.lifespan():
        host.mcp.httpx_client_factory = into(transport)
        for username in ("alice", "bob"):
            user = await a_user(username, profiles={"other": username})
            profile = await host.users.profile("other", username)
            installed = await host.mcp.install(
                user.id,
                McpInstallRequest(
                    name="Feishu",
                    namespace="lark",
                    url=AnyHttpUrl(channel.upstream),
                    tentacle_id="lark",
                ),
            )
            with pytest.raises(McpUnavailable):
                await host.mcp.catalog(a_turn(profile), "personal/lark")
            authorization = await host.oauth.start(user, "lark", mcp_id=installed.id)
            assert isinstance(authorization, AuthorizationLink)
            async with browser(host.oauth) as http:
                opened = await http.get(str(authorization.authorization_uri))
                query = parse_qs(urlsplit(opened.headers["location"]).query)
                assert query["client_id"] == ["cli-dev"]
                assert query["code_challenge_method"] == ["S256"]
                pending = await host.oauth.staged_authorization(
                    "lark", authorization.operation_id
                )
                assert pending.code_verifier is not None
                challenge = (
                    urlsafe_b64encode(
                        sha256(
                            pending.code_verifier.get_secret_value().encode()
                        ).digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
                assert query["code_challenge"] == [challenge]
                returned = await http.get(
                    "/oauth/lark/callback",
                    params={"code": username, "state": query["state"][0]},
                )
            assert returned.status_code == 200
            linked = await host.users.profile("lark", f"ou_{username}")
            assert linked is not None
            assert linked.user_id == user.id
            catalog = await host.mcp.catalog(a_turn(profile), "personal/lark")
            assert [tool.name for tool in catalog.tools] == ["get-user"]
            async with host.mcp.acquire(a_turn(profile), "personal/lark") as client:
                await client.call_tool("get-user", {})

    assert [headers["x-lark-mcp-uat"] for headers in seen] == [
        "refreshed-alice",
        "refreshed-bob",
    ]
    assert all(headers["x-lark-mcp-allowed-tools"] == "get-user" for headers in seen)
    assert all("authorization" not in headers for headers in seen)


async def test_custom_auth_still_refuses_another_destination(
    channel: LarkTentacle,
) -> None:
    auth = LarkMcpBearerAuth(
        manager=channel.octomate.oauth,
        user=User(username="alice"),
        mcp_id=uuid7(),
        url=channel.upstream,
        tentacle=channel,
    )
    async with httpx2.AsyncClient(auth=auth) as client:
        with pytest.raises(ValueError, match="bound to the installed MCP URL"):
            await client.get("https://other.example/mcp")


async def test_profile_linking_refuses_a_failed_identity_response(
    channel: LarkTentacle,
) -> None:
    connector = channel.octomate.oauth.connector("lark")
    assert isinstance(connector, LarkOAuthConnector)
    connector = connector.model_copy(
        update={
            "httpx_client_factory": into(
                httpx2.MockTransport(
                    lambda request: httpx2.Response(
                        200, json={"code": 999, "msg": "invalid token"}
                    )
                )
            )
        }
    )
    with pytest.raises(ValidationError):
        await connector.resolve_profile(OAuthGrant(access_token=SecretStr("invalid")))
