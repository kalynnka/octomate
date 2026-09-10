from __future__ import annotations

import uuid
from base64 import b64decode, urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import AnyHttpUrl, AnyUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OAuthMcpConfig, OctomateConfig
from octomate.config.mcp.base import AuthorizationCodeFlowConfig
from octomate.config.oauth import OAuthConfig
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.managers.user import UserManager
from octomate.oauth.mcp import (
    McpDeviceOAuthFlow,
    McpOAuthFlow,
    OAuthRefreshRejected,
    OAuthTokenExchange,
)
from octomate.schemas.mcp import McpInstallRequest, OAuthMcp
from octomate.schemas.oauth import (
    AuthorizationLink,
    DeviceAuthorization,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)
from octomate.schemas.user import User
from octomate.tentacles.mcp import build_mcp
from tests.support.users import a_user


def context() -> OAuthFlowContext:
    return OAuthFlowContext(
        operation_id=uuid.uuid4(),
        connector_id="work",
        user=User(username="alice"),
        profile=None,
    )


def device_flow(transport: httpx2.MockTransport) -> McpDeviceOAuthFlow:
    tokens = OAuthTokenExchange(
        token_endpoint=AnyUrl("https://auth.example/token"),
        client_id="test-app",
        client_secret=None,
        token_endpoint_auth_method="none",
        scopes=["repo", "read:org"],
        scope_separator=",",
        invalid_credentials_errors=[
            "invalid_grant",
            "invalid_client",
            "bad_refresh_token",
        ],
        httpx_client_factory=lambda headers=None, timeout=None, auth=None: (
            httpx2.AsyncClient(
                transport=transport, headers=headers, timeout=timeout, auth=auth
            )
        ),
    )
    return McpDeviceOAuthFlow(
        device_authorization_endpoint=AnyUrl("https://auth.example/device"),
        tokens=tokens,
    )


async def test_device_start_and_exchange_use_configured_endpoints() -> None:
    paths: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        form = parse_qs(request.content.decode())
        assert request.headers["Accept"] == "application/json"
        assert form["client_id"] == ["test-app"]
        if request.url.path == "/device":
            assert form["scope"] == ["repo read:org"]
            return httpx2.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.example/verify",
                    "verification_uri_complete": "https://auth.example/verify?code=ABCD-EFGH",
                    "expires_in": 900,
                },
            )
        assert form["device_code"] == ["device-secret"]
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
        return httpx2.Response(
            200,
            json={
                "access_token": "user-token",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "scope": "repo,read:org",
            },
        )

    flow = device_flow(httpx2.MockTransport(respond))
    authorization = await flow.start(context())
    assert authorization.interval_seconds == 5
    assert authorization.verification_uri_complete is not None
    assert "device-secret" not in authorization.model_dump_json()
    result = await flow.complete(context(), authorization.device_code)
    assert isinstance(result, OAuthGrant)
    assert result.scopes == ["repo", "read:org"]
    assert result.subject is None
    assert result.account_label is None
    assert result.refresh_token == SecretStr("refresh-secret")
    assert result.expires_at is not None
    assert result.expires_at > datetime.now(UTC)
    assert paths == ["/device", "/token"]


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(
    ("error", "expected"), [("authorization_pending", 12), ("slow_down", 17)]
)
async def test_device_pending_preserves_or_increases_poll_interval(
    status: int, error: str, expected: int
) -> None:
    flow = device_flow(
        httpx2.MockTransport(
            lambda request: httpx2.Response(status, json={"error": error})
        )
    )
    result = await flow.complete(
        replace(context(), interval_seconds=12), SecretStr("device-secret")
    )
    assert result == OAuthPending(retry_after_seconds=expected)


@pytest.mark.parametrize(
    "method", ["none", "client_secret_post", "client_secret_basic"]
)
async def test_authorization_code_pkce_and_client_authentication(method: str) -> None:
    callback = AnyHttpUrl("http://localhost/oauth/work/callback")
    forms: list[dict[str, list[str]]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url == httpx2.URL("https://auth.example/token")
        form = parse_qs(request.content.decode())
        forms.append(form)
        if method == "client_secret_basic":
            assert (
                b64decode(request.headers["Authorization"].removeprefix("Basic "))
                == b"test-app:s%3Aecret"
            )
            assert "client_secret" not in form
            assert "client_id" not in form
        elif method == "client_secret_post":
            assert form["client_secret"] == ["s:ecret"]
        else:
            assert "client_secret" not in form
            assert "Authorization" not in request.headers
        return httpx2.Response(200, json={"access_token": "token", "expires_in": 3600})

    tokens = OAuthTokenExchange(
        token_endpoint=AnyUrl("https://auth.example/token"),
        client_id="test-app",
        client_secret=SecretStr("s:ecret") if method != "none" else None,
        token_endpoint_auth_method=method,
        scopes=["read", "write"],
        scope_separator=" ",
        invalid_credentials_errors=["invalid_grant", "invalid_client"],
        httpx_client_factory=lambda headers=None, timeout=None, auth=None: (
            httpx2.AsyncClient(
                transport=httpx2.MockTransport(respond),
                headers=headers,
                timeout=timeout,
                auth=auth,
            )
        ),
    )
    flow = McpOAuthFlow(
        url=AnyUrl("https://mcp.example/mcp"),
        authorization_endpoint=AnyUrl("https://auth.example/authorize"),
        tokens=tokens,
        httpx_client_factory=tokens.httpx_client_factory,
    )
    authorization = await flow.start(context(), callback, SecretStr("state-secret"))
    params = parse_qs(authorization.authorization_uri.query or "")
    assert authorization.mcp_oauth is None
    assert "resource" not in params
    assert params["state"] == ["state-secret"]
    assert params["redirect_uri"] == [str(callback)]
    assert params["scope"] == ["read write"]
    assert params["code_challenge_method"] == ["S256"]
    assert authorization.code_verifier is not None
    challenge = (
        urlsafe_b64encode(
            sha256(authorization.code_verifier.get_secret_value().encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    assert params["code_challenge"] == [challenge]
    assert "s:ecret" not in str(authorization.authorization_uri)
    grant = await flow.exchange(
        context(),
        code="code",
        code_verifier=authorization.code_verifier,
        callback_uri=callback,
    )
    assert forms[0]["code_verifier"] == [authorization.code_verifier.get_secret_value()]
    assert forms[0]["redirect_uri"] == [str(callback)]
    assert "resource" not in forms[0]
    assert grant.mcp_oauth is None
    assert grant.scopes == ["read", "write"]
    refreshed = await flow.refresh(SecretStr("refresh-secret"))
    assert forms[1]["grant_type"] == ["refresh_token"]
    assert refreshed.refresh_token == SecretStr("refresh-secret")


@pytest.mark.parametrize("status", [200, 400, 401])
@pytest.mark.parametrize("error", ["invalid_grant", "bad_refresh_token"])
async def test_explicit_refresh_rejection(status: int, error: str) -> None:
    flow = device_flow(
        httpx2.MockTransport(
            lambda request: httpx2.Response(status, json={"error": error})
        )
    )
    with pytest.raises(OAuthRefreshRejected):
        await flow.refresh(SecretStr("spent"))


async def test_device_polling_and_refresh_through_manager(
    in_memory_engine: AsyncEngine,
) -> None:
    polls = 0
    refreshes = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal polls, refreshes
        form = parse_qs(request.content.decode())
        if request.url.path == "/device":
            return httpx2.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.example/verify",
                    "expires_in": 900,
                },
            )
        if form["grant_type"] == ["refresh_token"]:
            refreshes += 1
            if refreshes == 1:
                return httpx2.Response(503, json={"error": "temporarily_unavailable"})
            return httpx2.Response(
                200, json={"access_token": "new-token", "expires_in": 3600}
            )
        polls += 1
        if polls < 3:
            return httpx2.Response(400, json={"error": "slow_down"})
        return httpx2.Response(
            200,
            json={
                "access_token": "old-token",
                "refresh_token": "refresh-secret",
                "expires_in": 1,
            },
        )

    manager = OAuthManager(
        users=UserManager(),
        encryption_key=SecretStr(urlsafe_b64encode(bytes(range(32))).decode()),
        connectors=[
            OAuthConnector(id="work", flow=device_flow(httpx2.MockTransport(respond)))
        ],
    )
    owner = await a_user("alice")
    authorization = await manager.start(owner, "work")
    assert isinstance(authorization, DeviceAuthorization)
    assert await manager.complete(owner, authorization.operation_id) == OAuthPending(
        retry_after_seconds=10
    )
    assert await manager.complete(owner, authorization.operation_id) == OAuthPending(
        retry_after_seconds=15
    )
    assert isinstance(
        await manager.complete(owner, authorization.operation_id), OAuthGrant
    )
    with pytest.raises(httpx2.HTTPStatusError):
        await manager.access_token(owner, "work")
    assert await manager.access_token(owner, "work") == SecretStr("new-token")
    assert refreshes == 2


async def test_configured_callback_keeps_overlapping_users_separate(
    in_memory_engine: AsyncEngine,
) -> None:
    requests: list[dict[str, list[str]]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://auth.example/token"
        form = parse_qs(request.content.decode())
        requests.append(form)
        assert form["client_secret"] == ["app-secret"]
        assert "resource" not in form
        assert "refresh_token" in form or "code_verifier" in form
        if "code" in form:
            owner = form["code"][0]
            return httpx2.Response(
                200,
                json={
                    "access_token": owner + "-access",
                    "refresh_token": owner + "-refresh",
                    "expires_in": 1,
                },
            )
        return httpx2.Response(
            200,
            json={
                "access_token": form["refresh_token"][0] + "-renewed",
                "expires_in": 3600,
            },
        )

    host = Octomate(
        config=OctomateConfig(
            oauth=OAuthConfig(
                callback_base_uri=AnyHttpUrl("https://octomate.example"),
            )
        ),
        oauth_encryption_key=SecretStr(urlsafe_b64encode(bytes(range(32))).decode()),
    )
    host.oauth.httpx_client_factory = lambda headers=None, timeout=None, auth=None: (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(respond),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )
    )
    host.connect(
        build_mcp(
            "work",
            OAuthMcpConfig(
                url=AnyUrl("https://mcp.example/mcp"),
                client_id="app",
                client_secret=SecretStr("app-secret"),
                token_endpoint_auth_method="client_secret_post",
                scopes=["read"],
                flow=AuthorizationCodeFlowConfig(
                    authorization_endpoint=AnyUrl("https://auth.example/authorize"),
                    token_endpoint=AnyUrl("https://auth.example/token"),
                ),
            ),
            host,
        )
    )
    assert type(host.oauth.connector("work").flow) is McpOAuthFlow
    alice, bob = await a_user("alice"), await a_user("bob")
    assert [entry.id for entry in host.mcp.available()] == ["work"]
    assert await host.mcp.list(alice.id) == []
    assert "app-secret" not in host.mcp.available()[0].model_dump_json()
    first_mcp, second_mcp = [
        await host.mcp.install(
            owner.id,
            McpInstallRequest(
                name="Work",
                namespace="work",
                url=AnyUrl("https://mcp.example/mcp"),
                tentacle_id="work",
            ),
        )
        for owner in (alice, bob)
    ]
    assert isinstance(first_mcp, OAuthMcp)
    assert first_mcp.tentacle_id == "work"
    assert first_mcp.id != second_mcp.id
    first = await host.mcp.connect(alice, first_mcp.id)
    second = await host.mcp.connect(bob, second_mcp.id)
    assert isinstance(first, AuthorizationLink)
    assert isinstance(second, AuthorizationLink)
    first_payload = await host.oauth.staged_authorization("work", first.operation_id)
    second_payload = await host.oauth.staged_authorization("work", second.operation_id)
    assert first_payload.code_verifier != second_payload.code_verifier
    for owner, payload in [(bob, second_payload), (alice, first_payload)]:
        assert payload.mcp_oauth is None
        grant = await host.oauth.complete_callback(
            "work", state=payload.state.get_secret_value(), code=owner.username
        )
        assert grant.access_token == SecretStr(owner.username + "-access")
        assert grant.mcp_oauth is None
        assert payload.code_verifier is not None
        assert requests[-1]["code_verifier"] == [
            payload.code_verifier.get_secret_value()
        ]
    assert await host.oauth.access_token(
        alice, "work", mcp_id=first_mcp.id
    ) == SecretStr("alice-refresh-renewed")
    assert await host.oauth.access_token(
        bob, "work", mcp_id=second_mcp.id
    ) == SecretStr("bob-refresh-renewed")
    assert len(requests) == 4
