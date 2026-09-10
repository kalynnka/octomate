from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.oauth import OAuthConfig
from octomate.database import async_session
from octomate.managers.mcp import McpClientKey, McpUnavailable
from octomate.managers.oauth import OAuthConnector, OAuthLockKey, UnusableOAuthOperation
from octomate.oauth.base import McpBearerAuth
from octomate.schemas.mcp import McpInstallRequest, OAuth, OAuthMcp
from octomate.schemas.oauth import (
    AuthorizationLink,
    DeviceAuthorization,
    OAuthConnection,
    OAuthGrant,
    OAuthOperation,
    OAuthPending,
    OAuthTokenPayload,
)
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.mcp import OAuthMcpTentacle
from octomate.types.oauth import HttpsUrl
from tests.agent.test_mcp import ENCRYPTION_KEY, a_turn, an_upstream
from tests.managers.test_mcp import connected
from tests.managers.test_oauth import FakeDeviceFlow
from tests.support.users import a_user, auth_config

URL = "https://mcp.example/mcp"
ISSUER = "https://auth.example"


class OAuthServer:
    def __init__(self) -> None:
        self.registrations: int = 0
        self.tokens: list[dict[str, list[str]]] = []
        self.refresh_status: int = 200
        self.issued_refresh_token: bool = True
        self.registration: bool = True
        self.issuer: str = ISSUER
        self.resource: str = URL
        self.requires_issuer: bool = True
        self.registration_secret: str | None = None
        self.cimd: bool = False
        self.token_error: bool = False

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        match request.url.path:
            case "/mcp":
                assert request.headers.get("authorization") is None
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp", scope="read write"',
                    },
                )
            case "/.well-known/oauth-protected-resource/mcp":
                return httpx2.Response(
                    200,
                    json={
                        "resource": self.resource,
                        "authorization_servers": [ISSUER],
                        "scopes_supported": ["read", "write"],
                    },
                )
            case "/.well-known/oauth-authorization-server":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": self.issuer,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "registration_endpoint": f"{ISSUER}/register"
                        if self.registration
                        else None,
                        "response_types_supported": ["code"],
                        "code_challenge_methods_supported": ["S256"],
                        "authorization_response_iss_parameter_supported": self.requires_issuer,
                        "scopes_supported": ["read", "write", "offline_access"],
                        "client_id_metadata_document_supported": self.cimd,
                    },
                )
            case "/register":
                metadata = OAuthClientMetadata.model_validate_json(request.content)
                self.registrations += 1
                return httpx2.Response(
                    201,
                    json={
                        "client_id": f"client-{self.registrations}",
                        "client_secret": self.registration_secret,
                        "redirect_uris": [
                            str(uri) for uri in metadata.redirect_uris or []
                        ],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "client_secret_post"
                        if self.registration_secret
                        else "none",
                    },
                )
            case "/token":
                data = parse_qs(request.content.decode())
                self.tokens.append(data)
                if self.token_error:
                    return httpx2.Response(503)
                assert data["resource"] == [URL]
                if self.registration_secret is not None:
                    assert data["client_secret"] == [self.registration_secret]
                if data["grant_type"] == ["refresh_token"]:
                    if self.refresh_status != 200:
                        return httpx2.Response(
                            self.refresh_status, json={"error": "invalid_grant"}
                        )
                    code = (
                        data["refresh_token"][0].removesuffix("-refresh") + "-renewed"
                    )
                else:
                    assert len(data["code_verifier"][0]) >= 43
                    assert data["redirect_uri"] == [
                        "https://octomate.example/oauth/mcp/callback"
                    ]
                    code = data["code"][0]
                return httpx2.Response(
                    200,
                    json={
                        "access_token": code + "-access",
                        "refresh_token": code + "-refresh"
                        if self.issued_refresh_token
                        else None,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            case _:
                pytest.fail(f"Unexpected OAuth request: {request.method} {request.url}")

    def client(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = False,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(self.respond), headers=headers, auth=auth
        )


@pytest.fixture
def upstream() -> OAuthServer:
    return OAuthServer()


@pytest.fixture
def host(in_memory_engine: AsyncEngine, upstream: OAuthServer) -> Octomate:
    host = Octomate(
        config=OctomateConfig(
            auth=auth_config(),
            oauth=OAuthConfig(callback_base_uri=AnyHttpUrl("https://octomate.example")),
        ),
        oauth_encryption_key=ENCRYPTION_KEY,
    )
    host.oauth.httpx_client_factory = upstream.client
    return host


async def install(
    host: Octomate, user: User, namespace: str, tentacle_id: str | None = None
) -> OAuthMcp:
    if tentacle_id is not None:
        tentacle = OAuthMcpTentacle(id=tentacle_id, octomate=host)
        tentacle.label = tentacle_id
        tentacle.upstream = URL
        tentacle.instructions = ""
        host.mcp.tentacles[tentacle.id] = tentacle
    instance = await host.mcp.install(
        user.id,
        McpInstallRequest(
            name=namespace,
            namespace=namespace,
            url=AnyHttpUrl(URL),
            auth=OAuth() if tentacle_id is None else None,
            tentacle_id=tentacle_id,
        ),
    )
    assert isinstance(instance, OAuthMcp)
    return instance


async def authorize(
    host: Octomate, user: User, instance: OAuthMcp, code: str
) -> OAuthGrant:
    authorization = await host.mcp.connect(user, instance.id)
    assert isinstance(authorization, AuthorizationLink)
    payload = await host.oauth.staged_authorization("mcp", authorization.operation_id)
    assert "state=" not in str(authorization.authorization_uri)
    assert payload.mcp_oauth is not None
    return await host.oauth.complete_callback(
        "mcp", state=payload.state.get_secret_value(), code=code, issuer=ISSUER
    )


async def connection(instance: OAuthMcp) -> OAuthConnection:
    async with async_session() as session:
        stored = await session.one_or_none(
            OAuthConnection, expressions=[OAuthConnection["mcp_id"] == instance.id]
        )
    assert stored is not None
    return stored


async def expire(instance: OAuthMcp) -> None:
    async with async_session() as session:
        stored = await session.get(OAuthConnection, (await connection(instance)).id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


async def test_same_url_has_independent_user_and_workspace_grants(
    host: Octomate, upstream: OAuthServer
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    first = await install(host, alice, "linear_kalynnka")
    second = await install(host, alice, "linear_streamify")
    third = await install(host, bob, "linear_kalynnka")
    for user, instance, code in [
        (alice, first, "kalynnka"),
        (alice, second, "streamify"),
        (bob, third, "bob"),
    ]:
        grant = await authorize(host, user, instance, code)
        assert grant.subject is None
        assert grant.account_label is None
        assert (await host.mcp.confirm(user, instance.id)).status == "active"
        token = await host.oauth.access_token(user, "mcp", mcp_id=instance.id)
        assert token == SecretStr(code + "-access")
        assert code.encode() not in (await connection(instance)).encrypted_tokens
    assert upstream.registrations == 3
    assert await host.oauth.access_token(alice, "mcp") is None
    assert await host.oauth.access_token(bob, "mcp", mcp_id=first.id) is None
    with pytest.raises(McpUnavailable):
        await host.mcp.connect(bob, first.id)
    assert (await connection(first)).id != (await connection(second)).id
    async with async_session() as session:
        duplicate = OAuthConnection(
            user_id=alice.id,
            connector_id="another",
            mcp_id=first.id,
            encrypted_tokens=b"duplicate",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_registration_secrets_persist_encrypted_and_survive_restart(
    host: Octomate, upstream: OAuthServer
) -> None:
    user = await a_user()
    instance = await install(host, user, "logfire")
    upstream.registration_secret = "dynamically-issued-client-secret"
    await authorize(host, user, instance, "logfire")
    stored = await connection(instance)
    assert upstream.registration_secret.encode() not in stored.encrypted_tokens
    assert host.oauth.cipher is not None
    payload = OAuthTokenPayload.model_validate_json(
        host.oauth.cipher.decrypt(
            stored.encrypted_tokens, context=f"connection:{stored.id}"
        )
    )
    assert payload.mcp_oauth is not None
    assert payload.mcp_oauth.client.client_secret == upstream.registration_secret
    await expire(instance)
    restarted = Octomate(config=host.config, oauth_encryption_key=ENCRYPTION_KEY)
    restarted.oauth.httpx_client_factory = upstream.client
    token = await restarted.oauth.access_token(user, "mcp", mcp_id=instance.id)
    assert token == SecretStr("logfire-renewed-access")
    assert upstream.registrations == 1


async def test_refresh_is_serialized_and_retains_an_omitted_refresh_token(
    host: Octomate, upstream: OAuthServer
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    await authorize(host, user, instance, "linear")
    await expire(instance)
    upstream.issued_refresh_token = False
    tokens = await asyncio.gather(
        *(host.oauth.access_token(user, "mcp", mcp_id=instance.id) for _ in range(5))
    )
    assert tokens == [SecretStr("linear-renewed-access")] * 5
    assert len(upstream.tokens) == 2
    await expire(instance)
    assert await host.oauth.access_token(user, "mcp", mcp_id=instance.id) == SecretStr(
        "linear-renewed-access"
    )
    assert upstream.tokens[-1]["refresh_token"] == ["linear-refresh"]


@pytest.mark.parametrize("status", [400, 500])
async def test_refresh_rejection_differs_from_network_failure(
    host: Octomate, upstream: OAuthServer, status: int
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    await authorize(host, user, instance, "linear")
    await expire(instance)
    upstream.refresh_status = status
    if status == 400:
        assert await host.oauth.access_token(user, "mcp", mcp_id=instance.id) is None
        assert (await connection(instance)).status == "invalid"
    else:
        with pytest.raises(httpx2.HTTPStatusError):
            await host.oauth.access_token(user, "mcp", mcp_id=instance.id)
        assert (await connection(instance)).status == "active"


async def test_callback_validates_issuer_and_cannot_be_replayed(
    host: Octomate, upstream: OAuthServer
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    authorization = await host.mcp.connect(user, instance.id)
    assert isinstance(authorization, AuthorizationLink)
    payload = await host.oauth.staged_authorization("mcp", authorization.operation_id)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=host), base_url="https://octomate.example"
    ) as client:
        params = {
            "state": payload.state.get_secret_value(),
            "code": "linear",
            "iss": "https://wrong.example",
        }
        assert (
            await client.get("/oauth/mcp/callback", params=params)
        ).status_code == 400
        assert upstream.tokens == []
        params["iss"] = ISSUER
        assert (
            await client.get("/oauth/mcp/callback", params=params)
        ).status_code == 200
        assert (
            await client.get("/oauth/mcp/callback", params=params)
        ).status_code == 404
        assert len(upstream.tokens) == 1


@pytest.mark.parametrize("remove", [False, True])
async def test_disable_or_uninstall_blocks_pending_callback(
    host: Octomate, upstream: OAuthServer, remove: bool
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    authorization = await host.mcp.connect(user, instance.id)
    assert isinstance(authorization, AuthorizationLink)
    payload = await host.oauth.staged_authorization("mcp", authorization.operation_id)
    if remove:
        await host.mcp.uninstall(user.id, instance.id)
    else:
        await host.mcp.disable(user.id, instance.id)
    with pytest.raises(UnusableOAuthOperation):
        await host.oauth.complete_callback(
            "mcp", state=payload.state.get_secret_value(), code="unused", issuer=ISSUER
        )
    assert upstream.tokens == []


async def test_pool_refreshes_tokens_and_keeps_grants_across_eviction(
    host: Octomate,
) -> None:
    user = await a_user()
    first = await install(host, user, "linear_kalynnka")
    second = await install(host, user, "linear_streamify")
    await authorize(host, user, first, "kalynnka")
    await authorize(host, user, second, "streamify")
    server, calls = an_upstream("answer")
    scope = a_turn(UserProfile(user_id=user.id))
    async with connected(host.mcp, server):
        async with host.mcp.acquire(scope, first.namespace) as original:
            await original.call_tool("answer", {})
        async with host.mcp.acquire(scope, second.namespace) as other:
            await other.call_tool("answer", {})
        assert original is not other
        await expire(first)
        async with host.mcp.acquire(scope, first.namespace) as reused:
            assert reused is original
            await reused.call_tool("answer", {})
        async with host.mcp.lock(first.id):
            await host.mcp.evict(McpClientKey(user.id, first.id))
        async with host.mcp.acquire(scope, first.namespace) as replacement:
            assert replacement is not original
            await replacement.call_tool("answer", {})
        await host.mcp.disable(user.id, first.id)
        assert (await connection(first)).status == "active"
        await host.mcp.uninstall(user.id, second.id)
        async with async_session() as session:
            assert (
                await session.one_or_none(
                    OAuthConnection,
                    expressions=[OAuthConnection["mcp_id"] == second.id],
                )
                is None
            )
            assert (
                await session.one_or_none(
                    OAuthOperation, expressions=[OAuthOperation["mcp_id"] == second.id]
                )
                is None
            )
    assert calls == [
        "Bearer kalynnka-access",
        "Bearer streamify-access",
        "Bearer kalynnka-renewed-access",
        "Bearer kalynnka-renewed-access",
    ]


@pytest.mark.parametrize("reauthorize", [False, True])
async def test_unauthorized_call_is_not_replayed_and_stale_401_keeps_new_grant(
    host: Octomate,
    reauthorize: bool,
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    await authorize(host, user, instance, "old")
    requests: list[httpx2.Request] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if reauthorize:
            await authorize(host, user, instance, "new")
        return httpx2.Response(401)

    auth = McpBearerAuth(
        manager=host.oauth, user=user, mcp_id=instance.id, connector_id="mcp", url=URL
    )
    async with httpx2.AsyncClient(
        auth=auth, transport=httpx2.MockTransport(respond)
    ) as client:
        assert (await client.post(URL)).status_code == 401
    assert len(requests) == 1
    assert await host.oauth.access_token(user, "mcp", mcp_id=instance.id) == (
        SecretStr("new-access") if reauthorize else None
    )


async def test_one_tentacle_app_can_authorize_two_installed_mcps(
    host: Octomate,
) -> None:
    user = await a_user()
    flow = FakeDeviceFlow()
    host.oauth.register(
        OAuthConnector.model_validate({"id": "github", "flow": flow, "mcp_url": URL})
    )
    first = await install(host, user, "github_work", "github")
    second = await install(host, user, "github_personal", "github")
    for instance, token in [(first, "work"), (second, "personal")]:
        pending = await host.mcp.connect(user, instance.id)
        assert isinstance(pending, DeviceAuthorization)
        flow.completion = OAuthGrant(access_token=SecretStr(token))
        assert (await host.mcp.confirm(user, instance.id)).status == "active"
    assert await host.oauth.access_token(user, "github", mcp_id=first.id) == SecretStr(
        "work"
    )
    assert await host.oauth.access_token(user, "github", mcp_id=second.id) == SecretStr(
        "personal"
    )
    assert await host.oauth.access_token(user, "github") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://evil.example"),
        ("resource", "https://other.example/mcp"),
        ("registration", False),
    ],
)
async def test_rejects_wrong_metadata_or_manual_app_requirement(
    host: Octomate, upstream: OAuthServer, field: str, value: str | bool
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    setattr(upstream, field, value)
    with pytest.raises(
        (ValueError, OAuthFlowError), match=r"issuer|resource|McpTentacle"
    ):
        await host.mcp.connect(user, instance.id)
    assert upstream.registrations == 0
    assert upstream.tokens == []


async def test_cimd_skips_registration_when_supported(
    host: Octomate, upstream: OAuthServer
) -> None:
    user = await a_user()
    upstream.cimd = True
    upstream.registration = False
    host.oauth.client_metadata_url = TypeAdapter(HttpsUrl).validate_python(
        "https://octomate.example/oauth/client.json"
    )
    instance = await install(host, user, "linear")
    await authorize(host, user, instance, "cimd")
    assert upstream.registrations == 0
    assert upstream.tokens[0]["client_id"] == [str(host.oauth.client_metadata_url)]


async def test_failed_exchange_consumes_callback_without_replaying_it(
    host: Octomate, upstream: OAuthServer
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    authorization = await host.mcp.connect(user, instance.id)
    assert isinstance(authorization, AuthorizationLink)
    payload = await host.oauth.staged_authorization("mcp", authorization.operation_id)
    upstream.token_error = True
    with pytest.raises(httpx2.HTTPStatusError):
        await host.oauth.complete_callback(
            "mcp", state=payload.state.get_secret_value(), code="once", issuer=ISSUER
        )
    with pytest.raises(UnusableOAuthOperation):
        await host.oauth.complete_callback(
            "mcp", state=payload.state.get_secret_value(), code="once", issuer=ISSUER
        )
    assert len(upstream.tokens) == 1


async def test_denial_issuer_is_checked_before_consuming_authorization(
    host: Octomate,
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    authorization = await host.mcp.connect(user, instance.id)
    assert isinstance(authorization, AuthorizationLink)
    payload = await host.oauth.staged_authorization("mcp", authorization.operation_id)
    with pytest.raises(OAuthFlowError):
        await host.oauth.abandon_callback(
            "mcp",
            state=payload.state.get_secret_value(),
            issuer="https://wrong.example",
        )
    await host.oauth.abandon_callback(
        "mcp", state=payload.state.get_secret_value(), issuer=ISSUER
    )
    with pytest.raises(UnusableOAuthOperation):
        await host.oauth.staged_authorization("mcp", authorization.operation_id)


async def test_management_api_authorizes_only_the_logged_in_users_mcp(
    host: Octomate,
) -> None:
    user, other = await a_user("alice"), await a_user("bob")
    assert host.auth is not None
    async with async_session() as session:
        alice = await session.get(User, user.id)
        assert alice is not None
        alice.password_hash = await host.auth.hash_password(
            SecretStr("password-for-test")
        )
        await session.commit()
    foreign = await install(host, other, "linear_other")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=host),
        base_url="https://octomate.example",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        logged_in = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "password-for-test"},
        )
        assert logged_in.status_code == 204
        base = "/api/mcp"
        installed = await client.post(
            base,
            json={
                "name": "Linear Kalynnka",
                "namespace": "linear_kalynnka",
                "url": URL,
                "auth": {"kind": "oauth"},
            },
        )
        assert installed.status_code == 201
        instance = OAuthMcp.model_validate(installed.json() | {"user_id": user.id})
        assert (await client.post(f"{base}/{foreign.id}/connect")).status_code == 404
        assert (await client.post(f"{base}/{foreign.id}/confirm")).status_code == 404
        assert (await client.post(f"{base}/{instance.id}/confirm")).json() == {
            "status": None
        }
        started = await client.post(f"{base}/{instance.id}/connect")
        assert started.status_code == 200
        assert "state=" not in started.text
        assert "client_secret" not in started.text
        authorization = AuthorizationLink.model_validate_json(started.content)
        waiting = await client.post(f"{base}/{instance.id}/confirm")
        assert waiting.json() == {"status": "pending_browser"}
        redirect = await client.get(str(authorization.authorization_uri))
        assert redirect.status_code == 307
        state = parse_qs(httpx2.URL(redirect.headers["location"]).query.decode())[
            "state"
        ][0]
        finished = await client.get(
            "/oauth/mcp/callback",
            params={"state": state, "code": "alice", "iss": ISSUER},
        )
        assert finished.status_code == 200
        assert "alice-access" not in finished.text
        confirmed = await client.post(f"{base}/{instance.id}/confirm")
        assert confirmed.json() == {"status": "active"}
        assert "access_token" not in confirmed.text
        assert "encrypted_tokens" not in (await client.get(base)).text

        flow = FakeDeviceFlow()
        host.oauth.register(
            OAuthConnector.model_validate(
                {"id": "github", "flow": flow, "mcp_url": URL}
            )
        )
        device = await install(host, user, "github", "github")
        other_device = await install(host, other, "github", "github")
        assert (
            await client.post(f"{base}/{other_device.id}/connect")
        ).status_code == 404
        started = await client.post(f"{base}/{device.id}/connect")
        assert started.status_code == 200
        assert started.headers["cache-control"] == "no-store"
        assert started.json()["user_code"] == "ABCD-EFGH"
        assert "device-secret" not in started.text
        assert "github-token" not in started.text
        flow.completion = OAuthPending(retry_after_seconds=7)
        waiting = await client.post(f"{base}/{device.id}/confirm")
        assert waiting.json() == {
            "status": "pending_device",
            "retry_after_seconds": 7,
        }
        flow.completion = OAuthGrant(access_token=SecretStr("github-token"))
        assert (await client.post(f"{base}/{device.id}/confirm")).json() == {
            "status": "active"
        }
        await host.oauth.invalidate(user, "github", mcp_id=device.id)
        assert (await client.post(f"{base}/{device.id}/confirm")).json() == {
            "status": "invalid"
        }
        client.cookies.clear()
        assert (await client.post(f"{base}/{device.id}/connect")).status_code == 401


@pytest.mark.parametrize("uninstall", [False, True])
@pytest.mark.parametrize("tentacle_id", [None, "github"])
async def test_lifecycle_waits_for_the_matching_oauth_lock(
    host: Octomate, uninstall: bool, tentacle_id: str | None
) -> None:
    user = await a_user()
    if tentacle_id is not None:
        host.oauth.register(
            OAuthConnector.model_validate(
                {"id": tentacle_id, "flow": FakeDeviceFlow(), "mcp_url": URL}
            )
        )
    instance = await install(host, user, "provider", tentacle_id)
    key = OAuthLockKey(
        user_id=user.id, mcp_id=instance.id, connector_id=tentacle_id or "mcp"
    )
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        async with host.oauth.lock(key):
            if uninstall:
                operation = tasks.create_task(host.mcp.uninstall(user.id, instance.id))
            else:
                operation = tasks.create_task(host.mcp.disable(user.id, instance.id))
            done, _ = await asyncio.wait({operation}, timeout=0.05)
            assert not done
        await operation


@pytest.mark.parametrize("uninstall", [False, True])
async def test_shutdown_does_not_hold_the_oauth_lock(
    host: Octomate, monkeypatch: pytest.MonkeyPatch, uninstall: bool
) -> None:
    user = await a_user()
    instance = await install(host, user, "linear")
    await authorize(host, user, instance, "linear")
    server, _ = an_upstream("answer")
    async with connected(host.mcp, server):
        async with host.mcp.acquire(
            a_turn(UserProfile(user_id=user.id)), instance.namespace
        ) as client:
            await client.call_tool("answer", {})
        original_close = client.close
        closed: list[bool] = []

        async def close_with_auth_cleanup() -> None:
            # Transport shutdown can wait for an authentication task to finish.
            async with (
                asyncio.timeout(0.5),
                host.oauth.lock(
                    OAuthLockKey(
                        user_id=user.id,
                        mcp_id=instance.id,
                        connector_id=instance.tentacle_id or "mcp",
                    )
                ),
            ):
                await original_close()
                closed.append(True)

        monkeypatch.setattr(client, "close", close_with_auth_cleanup)
        if uninstall:
            await host.mcp.uninstall(user.id, instance.id)
        else:
            await host.mcp.disable(user.id, instance.id)
        assert closed == [True]
