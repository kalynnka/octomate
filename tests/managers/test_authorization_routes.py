"""Public-client OAuth exercised through HTTP, with real persisted grants."""

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import AnyHttpUrl, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.auth import auth_router
from octomate.authorization import discovery_router, router
from octomate.base import Octomate
from octomate.config.auth import AuthConfig
from octomate.config.base import OctomateConfig
from octomate.config.oauth import OAuthConfig
from octomate.database import async_session
from octomate.dependencies import auth_manager
from octomate.managers.auth import AuthManager
from octomate.oauth.design import design_router
from octomate.schemas.authorization import OAuthAuthorization, OAuthClient, OAuthGrant

ORIGIN = "https://octomate.test"
RESOURCE = f"{ORIGIN}/octomate/mcp"
REDIRECT = "http://127.0.0.1:54321/callback?keep=yes"
VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
BROWSER_HEADERS = {"X-Octomate-Request": "1"}


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def auth() -> AuthManager:
    return AuthManager(
        AuthConfig(
            access_token_salt=SecretStr("access-token-test-salt"),
            refresh_token_salt=SecretStr("refresh-token-test-salt"),
            api_key_salt=SecretStr("api-key-test-salt"),
            oauth_issuer_url=AnyHttpUrl(ORIGIN),
        )
    )


@pytest.fixture
async def browser(auth: AuthManager) -> AsyncIterator[httpx2.AsyncClient]:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(design_router)
    app.dependency_overrides[auth_manager] = lambda: auth
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        yield client


async def register(client: httpx2.AsyncClient, *, scope: str = "mcp") -> str:
    response = await client.post(
        "/oauth/register",
        json={
            "client_name": "Native harness",
            "redirect_uris": [REDIRECT],
            "scope": scope,
        },
    )
    assert response.status_code == 201, response.text
    assert "client_secret" not in response.json()
    return response.json()["client_id"]


async def start(client: httpx2.AsyncClient, client_id: str, **overrides: str) -> str:
    response = await client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "client-state",
            **overrides,
        },
    )
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert not location.query
    return parse_qs(location.fragment)["ticket"][0]


async def sign_in(client: httpx2.AsyncClient, auth: AuthManager) -> str:
    invitation = await auth.invite()
    response = await client.post(
        "/api/auth/register",
        headers=BROWSER_HEADERS,
        json={
            "username": "alice",
            "name": "Alice",
            "password": "Strong-password1!",
            "invitation": invitation.get_secret_value(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def approve(client: httpx2.AsyncClient, ticket: str, user_id: str) -> str:
    response = await client.post(
        "/oauth/consent/decide",
        headers=BROWSER_HEADERS,
        json={"ticket": ticket, "expected_user_id": user_id, "approve": True},
    )
    assert response.status_code == 200, response.text
    callback = parse_qs(urlsplit(response.json()["redirect_uri"]).query)
    assert callback["state"] == ["client-state"]
    assert callback["keep"] == ["yes"]
    assert callback["iss"] == [f"{ORIGIN}/"]
    return callback["code"][0]


async def exchange(
    client: httpx2.AsyncClient, client_id: str, code: str, **overrides: str
) -> httpx2.Response:
    return await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **overrides,
        },
    )


async def test_discovery_uses_configured_origin_not_host_header(
    browser: httpx2.AsyncClient,
) -> None:
    response = await browser.get(
        "/.well-known/oauth-authorization-server", headers={"Host": "attacker.test"}
    )
    data = response.json()
    assert data["issuer"] == f"{ORIGIN}/"
    assert data["token_endpoint"] == f"{ORIGIN}/oauth/token"
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert data["token_endpoint_auth_methods_supported"] == ["none"]
    for path, scopes in [("octomate/mcp", ["mcp"]), ("api", ["hooks", "acp"])]:
        metadata = await browser.get(f"/.well-known/oauth-protected-resource/{path}")
        assert metadata.json()["resource"] == f"{ORIGIN}/{path}"
        assert metadata.json()["scopes_supported"] == scopes
        assert metadata.json()["authorization_servers"] == [data["issuer"]]
    assert (
        await browser.get("/.well-known/oauth-protected-resource/unknown")
    ).status_code == 404


async def test_complete_flow_refresh_and_revocation(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    assert (
        await browser.post(
            "/oauth/consent/inspect",
            headers=BROWSER_HEADERS,
            json={"ticket": ticket},
        )
    ).status_code == 401
    page = await browser.get("/oauth/consent")
    assert page.status_code == 302
    assert page.headers["location"] == f"{ORIGIN}/auth/authorize"
    assert page.headers["cache-control"] == "no-store"
    assert ticket not in page.text
    user_id = await sign_in(browser, auth)
    inspect = await browser.post(
        "/oauth/consent/inspect", headers=BROWSER_HEADERS, json={"ticket": ticket}
    )
    assert inspect.json()["username"] == "alice"
    assert inspect.json()["client_name"] == "Native harness"
    code = await approve(browser, ticket, user_id)
    issued = await exchange(browser, client_id, code)
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    first = issued.json()
    assert first["scope"] == "mcp"
    assert 890 <= first["expires_in"] <= 900
    principal = await auth.authenticate_oauth_token(
        SecretStr(first["access_token"]), resource=RESOURCE, scope="mcp"
    )
    assert principal is not None
    assert str(principal.user_id) == user_id
    refreshed = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": first["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    # A separate manager sees the same persisted credentials after restart.
    restarted = AuthManager(auth.config)
    assert (
        await restarted.authenticate_oauth_token(
            SecretStr(second["access_token"]), resource=RESOURCE, scope="mcp"
        )
        is not None
    )
    revoked = await browser.post(
        "/oauth/revoke",
        data={"client_id": client_id, "token": second["refresh_token"]},
    )
    assert revoked.status_code == 200
    for bearer in [first["access_token"], second["access_token"]]:
        assert (
            await restarted.authenticate_oauth_token(
                SecretStr(bearer), resource=RESOURCE, scope="mcp"
            )
            is None
        )


@pytest.mark.parametrize(
    "redirect",
    [
        "http://remote.test/callback",
        "https://host.test/cb#fragment",
        "javascript:alert(1)",
        "https://user:pass@host.test/cb",
        "http://127.0.0.1:bad/cb",
        "http://[broken",
        "https://host.test/\\evil",
        "https://host.test/space here",
    ],
)
async def test_registration_rejects_unsafe_redirects(
    browser: httpx2.AsyncClient, redirect: str
) -> None:
    response = await browser.post("/oauth/register", json={"redirect_uris": [redirect]})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "override",
    [
        {"scope": "admin"},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["client_credentials"]},
        {"response_types": ["token"]},
        {"client_name": ""},
    ],
)
async def test_registration_rejects_unsupported_metadata(
    browser: httpx2.AsyncClient, override: dict[str, str | list[str]]
) -> None:
    response = await browser.post(
        "/oauth/register", json={"redirect_uris": [REDIRECT], **override}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"resource": "https://other.test/mcp"}, "invalid_target"),
        ({"scope": "hooks"}, "invalid_scope"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"redirect_uri": "http://127.0.0.1:54321/other?keep=yes"}, "invalid_request"),
        ({"redirect_uri": "http://[broken"}, "invalid_request"),
    ],
)
async def test_authorization_rejects_invalid_bindings(
    browser: httpx2.AsyncClient, override: dict[str, str], error: str
) -> None:
    client_id = await register(browser)
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            **override,
        },
    )
    if "redirect_uri" in override:
        assert response.status_code == 400
        assert response.json()["error"] == error
        assert "location" not in response.headers
    else:
        assert response.status_code == 302
        callback = parse_qs(urlsplit(response.headers["location"]).query)
        assert callback["error"] == [error]
        assert callback["iss"] == [f"{ORIGIN}/"]


async def test_loopback_port_can_change_but_exchange_is_exact(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    redirect = REDIRECT.replace("54321", "54322")
    ticket = await start(browser, client_id, redirect_uri=redirect)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    assert (await exchange(browser, client_id, code)).status_code == 400
    assert (
        await exchange(browser, client_id, code, redirect_uri=redirect)
    ).status_code == 200


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"code_verifier": "w" * 43}, "invalid_grant"),
        ({"code_verifier": "!" * 43}, "invalid_grant"),
        ({"redirect_uri": "https://attacker.test/cb"}, "invalid_grant"),
        ({"resource": f"{ORIGIN}/api"}, "invalid_grant"),
        ({"resource": "https://attacker.test/mcp"}, "invalid_target"),
    ],
)
async def test_failed_exchange_does_not_consume_code(
    browser: httpx2.AsyncClient, auth: AuthManager, override: dict[str, str], error: str
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    refused = await exchange(browser, client_id, code, **override)
    assert refused.status_code == 400
    assert refused.json()["error"] == error
    assert (await exchange(browser, client_id, code)).status_code == 200


@pytest.mark.parametrize("replay", ["code", "refresh"])
async def test_replay_revokes_family_through_http(
    browser: httpx2.AsyncClient, auth: AuthManager, replay: str
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    first = (await exchange(browser, client_id, code)).json()
    refresh = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": first["refresh_token"],
        "resource": RESOURCE,
    }
    second = (await browser.post("/oauth/token", data=refresh)).json()
    refused = (
        await exchange(browser, client_id, code)
        if replay == "code"
        else await browser.post("/oauth/token", data=refresh)
    )
    assert refused.json()["error"] == "invalid_grant"
    assert (
        await auth.authenticate_oauth_token(
            SecretStr(second["access_token"]), resource=RESOURCE, scope="mcp"
        )
        is None
    )


async def test_consent_requires_csrf_header_and_same_account_then_denial_is_final(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    user_id = await sign_in(browser, auth)
    body = {"ticket": ticket, "expected_user_id": user_id, "approve": True}
    assert (await browser.post("/oauth/consent/decide", json=body)).status_code == 403
    assert (
        await browser.post(
            "/oauth/consent/decide",
            headers=BROWSER_HEADERS,
            json={**body, "expected_user_id": str(uuid7())},
        )
    ).status_code == 409
    denied = await browser.post(
        "/oauth/consent/decide",
        headers=BROWSER_HEADERS,
        json={**body, "approve": False},
    )
    callback = parse_qs(urlsplit(denied.json()["redirect_uri"]).query)
    assert callback["error"] == ["access_denied"]
    assert callback["state"] == ["client-state"]
    assert "code" not in callback
    assert (
        await browser.post("/oauth/consent/decide", headers=BROWSER_HEADERS, json=body)
    ).status_code == 400
    async with async_session() as session:
        assert list(await session.list(OAuthGrant, limit=None)) == []


async def test_other_client_cannot_exchange_or_revoke(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    other = await register(browser)
    ticket = await start(browser, client_id)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    assert (await exchange(browser, other, code)).status_code == 400
    issued = (await exchange(browser, client_id, code)).json()
    response = await browser.post(
        "/oauth/revoke", data={"client_id": other, "token": issued["refresh_token"]}
    )
    assert response.status_code == 200
    assert (
        await auth.authenticate_oauth_token(
            SecretStr(issued["access_token"]), resource=RESOURCE, scope="mcp"
        )
        is not None
    )
    assert (
        await browser.post(
            "/oauth/revoke", data={"client_id": client_id, "token": "unknown"}
        )
    ).status_code == 200


async def test_duplicate_parameters_are_rejected(browser: httpx2.AsyncClient) -> None:
    assert (
        await browser.post(
            "/oauth/token",
            content="grant_type=refresh_token&grant_type=authorization_code",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    ).json()["error"] == "invalid_request"
    assert (await browser.get("/oauth/authorize?client_id=one&client_id=two")).json()[
        "error"
    ] == "invalid_request"


async def test_registration_and_pending_consent_store_no_raw_ticket(
    browser: httpx2.AsyncClient,
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    async with async_session() as session:
        clients = await session.list(OAuthClient, limit=None)
        pending = await session.list(OAuthAuthorization, limit=None)
    assert str(clients[0].id) == client_id
    assert clients[0].id.version == 7
    assert pending[0].ticket_hash.get_secret_value() != ticket
    assert ticket not in repr(pending[0])


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.test",
        "https://host.test/path",
        "https://host.test?query=1",
        "https://host.test/#fragment",
        "https://user:pass@host.test",
    ],
)
@pytest.mark.parametrize("field", ["oauth_issuer_url", "oauth_ui_url"])
def test_oauth_configuration_rejects_non_origin_urls(
    auth: AuthManager, url: str, field: str
) -> None:
    with pytest.raises(ValidationError):
        AuthConfig.model_validate({**auth.config.model_dump(), field: url})


async def test_disabled_issuer_has_no_protocol(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    auth.config.oauth_issuer_url = None
    assert (
        await browser.get("/.well-known/oauth-authorization-server")
    ).status_code == 404


async def test_cli_resource_scope_narrowing_and_failed_refresh(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser, scope="hooks acp")
    resource = f"{ORIGIN}/api"
    ticket = await start(browser, client_id, resource=resource)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    issued = (await exchange(browser, client_id, code, resource=resource)).json()
    refresh = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": issued["refresh_token"],
        "resource": resource,
    }
    for overrides, error in [
        ({"resource": RESOURCE}, "invalid_grant"),
        ({"scope": "mcp"}, "invalid_scope"),
        ({"scope": ""}, "invalid_scope"),
        ({"resource": "https://other.test"}, "invalid_target"),
    ]:
        response = await browser.post("/oauth/token", data={**refresh, **overrides})
        assert response.status_code == 400
        assert response.json()["error"] == error
    rotated = await browser.post("/oauth/token", data={**refresh, "scope": "hooks"})
    assert rotated.status_code == 200
    assert rotated.json()["scope"] == "hooks"
    bearer = SecretStr(rotated.json()["access_token"])
    assert (
        await auth.authenticate_oauth_token(bearer, resource=resource, scope="hooks")
        is not None
    )
    assert (
        await auth.authenticate_oauth_token(bearer, resource=resource, scope="acp")
        is None
    )


async def test_omitted_redirect_is_inferred_and_cannot_be_added_at_exchange(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "client-state",
        },
    )
    ticket = parse_qs(urlsplit(response.headers["location"]).fragment)["ticket"][0]
    code = await approve(browser, ticket, await sign_in(browser, auth))
    assert (await exchange(browser, client_id, code)).status_code == 400
    issued = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        },
    )
    assert issued.status_code == 200


async def test_revoke_access_token_invalidates_refresh(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    code = await approve(browser, ticket, await sign_in(browser, auth))
    issued = (await exchange(browser, client_id, code)).json()
    for _ in range(2):
        revoked = await browser.post(
            "/oauth/revoke",
            data={
                "client_id": client_id,
                "token": issued["access_token"],
                "token_type_hint": "refresh_token",
            },
        )
        assert revoked.status_code == 200
    response = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": issued["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert response.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("callback_enabled", [True, False])
async def test_project_mounts_protocol_only_when_configured(
    auth: AuthManager, enabled: bool, callback_enabled: bool
) -> None:
    if not enabled:
        auth.config.oauth_issuer_url = None
    app = Octomate(
        config=OctomateConfig(
            auth=auth.config,
            oauth=OAuthConfig(
                callback_base_uri=AnyHttpUrl(ORIGIN) if callback_enabled else None,
            ),
        )
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        response = await client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == (200 if enabled else 404)
        consent = await client.get("/oauth/consent")
        assert consent.status_code == (302 if enabled else 404)
        assert (await client.get("/api/oauth/consent")).status_code == 404
        style = await client.get("/oauth/design/lonetrail.css")
        assert style.status_code == (200 if enabled or callback_enabled else 404)


async def test_concurrent_approval_and_denial_make_one_decision(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    client_id = await register(browser)
    ticket = await start(browser, client_id)
    user_id = await sign_in(browser, auth)
    results = await asyncio.gather(
        *(
            browser.post(
                "/oauth/consent/decide",
                headers=BROWSER_HEADERS,
                json={
                    "ticket": ticket,
                    "expected_user_id": user_id,
                    "approve": approve,
                },
            )
            for approve in [True, False]
        )
    )
    assert sorted(result.status_code for result in results) == [200, 400]


async def test_callback_retains_local_trunkline_design_assets(
    browser: httpx2.AsyncClient,
) -> None:
    stylesheet = await browser.get("/oauth/design/lonetrail.css")
    assert stylesheet.status_code == 200
    assert "--page-bg:" in stylesheet.text
    assert '[data-theme="dark"]' in stylesheet.text
    assert "@import url(" not in stylesheet.text
    assert 'url("/oauth/design/fonts/' in stylesheet.text
    font = await browser.get("/oauth/design/fonts/Novecentosanswide-DemiBold.otf")
    assert font.status_code == 200
    assert font.content.startswith(b"OTTO")
    for name in ["unknown.woff2", "lonetrail.css", "..%2F..%2Fauthorization.py"]:
        assert (await browser.get(f"/oauth/design/fonts/{name}")).status_code == 404


async def test_consent_redirects_to_configured_trunkline_origin(
    browser: httpx2.AsyncClient, auth: AuthManager
) -> None:
    auth.config.oauth_ui_url = AnyHttpUrl("https://console.test")
    response = await browser.get("/oauth/consent", headers={"host": "untrusted.test"})
    assert response.status_code == 302
    assert response.headers["location"] == "https://console.test/auth/authorize"
    assert response.headers["cache-control"] == "no-store"
