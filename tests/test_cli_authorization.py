"""CLI OAuth credentials authorize native hooks and ACP independently of MCP."""

from collections.abc import AsyncIterator
from contextvars import Context
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx2
import pytest
from fastapi import Depends, WebSocket
from fastapi.testclient import TestClient
from octomate_cli.tentacles.claude import CLAUDE_HOOK_PATH
from octomate_cli.tentacles.codex import CODEX_HOOK_PATH
from octomate_cli.tentacles.deepseek import DEEPSEEK_HOOK_PATH
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.schemas.authorization import OAuthGrant, OAuthToken
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.auth import acp_sender
from octomate.tentacles.acp.channel import AcpChannelTentacle
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.types.auth import ApiKeyScope
from tests.agent.core.test_hook_auth import EVENT, client_for
from tests.managers.test_authorization_routes import (
    ORIGIN,
    approve,
    exchange,
    register,
    sign_in,
    start,
)
from tests.support.users import a_user, auth_config

RESOURCE = f"{ORIGIN}/api"


@pytest.fixture
def app(in_memory_engine: AsyncEngine) -> Octomate:
    config = auth_config()
    config.oauth_issuer_url = AnyHttpUrl(ORIGIN)
    app = Octomate(config=OctomateConfig(auth=config))
    tentacle = app.connect(AcpChannelTentacle(id="bridge", octomate=app))
    resolve_hook = hook_sender(app.users, "claude", hook_guard(app.bearers))

    @app.get("/test/hooks")
    async def hook_identity(
        sender: Annotated[UserProfile, Depends(resolve_hook)],
    ) -> str:
        return sender.channel_user_id

    @app.websocket("/test/acp")
    async def acp_identity(
        websocket: WebSocket,
        sender: Annotated[UserProfile, Depends(acp_sender(tentacle))],
    ) -> None:
        await websocket.accept()
        await websocket.send_text(sender.channel_user_id)
        await websocket.close()

    return app


@pytest.fixture
async def browser(app: Octomate) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ORIGIN
    ) as browser:
        yield browser


@pytest.fixture
async def issued(app: Octomate, browser: httpx2.AsyncClient) -> httpx2.Response:
    assert app.auth is not None
    client_id = await register(browser, scope="hooks acp")
    user_id = await sign_in(browser, app.auth)
    ticket = await start(browser, client_id, resource=RESOURCE)
    code = await approve(browser, ticket, user_id)
    response = await exchange(browser, client_id, code, resource=RESOURCE)
    assert response.status_code == 200
    return response


async def test_cli_token_resolves_the_user_for_hooks_and_acp(
    app: Octomate, browser: httpx2.AsyncClient, issued: httpx2.Response
) -> None:
    tokens = issued.json()
    bearer = tokens["access_token"]
    principal = await app.bearers.verify_token(bearer, scope="acp")
    assert principal is not None
    assert principal.resource == RESOURCE
    assert principal.subject == "alice"
    await a_user(principal.client_id)
    headers = {"Authorization": f"Bearer {bearer}"}
    response = await browser.get("/test/hooks?username=someone-else", headers=headers)
    assert response.status_code == 200
    assert response.json() == "alice"

    def connect() -> None:
        with TestClient(app).websocket_connect(
            "/test/acp?username=someone-else", headers=headers
        ) as socket:
            assert socket.receive_text() == "alice"
        with TestClient(app).websocket_connect("/api/acp", headers=headers):
            pass

    Context().run(connect)
    assert await app.bearers.verify_token(bearer) is None
    refreshed = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": principal.client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200
    rotated = refreshed.json()
    assert await app.bearers.owner(rotated["access_token"], scope="hooks") == "alice"
    revoked = await browser.post(
        "/oauth/revoke",
        data={"client_id": principal.client_id, "token": rotated["refresh_token"]},
    )
    assert revoked.status_code == 200
    for access in (bearer, rotated["access_token"]):
        assert await app.bearers.owner(access, scope="hooks") is None
        assert await app.bearers.owner(access, scope="acp") is None
    assert (await browser.get("/test/hooks", headers=headers)).status_code == 401
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        TestClient(app).websocket_connect("/api/acp", headers=headers),
    ):
        pytest.fail("A revoked grant opened ACP")
    assert denied.value.status_code == 401


@pytest.mark.parametrize(
    "path", [CLAUDE_HOOK_PATH, CODEX_HOOK_PATH, DEEPSEEK_HOOK_PATH]
)
async def test_cli_token_opens_native_harness_hook_routes(
    app: Octomate, issued: httpx2.Response, path: str
) -> None:
    bearer = issued.json()["access_token"]
    response = client_for(path, app).post(
        path, json=EVENT, headers={"Authorization": f"Bearer {bearer}"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("scope", ["hooks", "acp"])
async def test_cli_scope_grants_only_the_requested_surface(
    app: Octomate, browser: httpx2.AsyncClient, scope: ApiKeyScope
) -> None:
    assert app.auth is not None
    client_id = await register(browser, scope=scope)
    user_id = await sign_in(browser, app.auth)
    ticket = await start(browser, client_id, resource=RESOURCE)
    code = await approve(browser, ticket, user_id)
    bearer = (await exchange(browser, client_id, code, resource=RESOURCE)).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {bearer}"}
    response = await browser.get("/test/hooks", headers=headers)
    assert response.status_code == (200 if scope == "hooks" else 401)
    if scope == "acp":
        with TestClient(app).websocket_connect("/api/acp", headers=headers):
            pass
    else:
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            TestClient(app).websocket_connect("/api/acp", headers=headers),
        ):
            pytest.fail("A hooks-only grant opened ACP")
        assert denied.value.status_code == 401
    assert await app.bearers.verify_token(bearer) is None


@pytest.mark.parametrize(
    "invalidity",
    ["token-expired", "grant-expired", "wrong-resource", "disabled", "refresh"],
)
async def test_inactive_or_wrong_credentials_cannot_open_hooks_or_acp(
    app: Octomate,
    browser: httpx2.AsyncClient,
    issued: httpx2.Response,
    invalidity: Literal[
        "token-expired", "grant-expired", "wrong-resource", "disabled", "refresh"
    ],
) -> None:
    assert app.auth is not None
    bearer = issued.json()["access_token"]
    async with async_session() as session:
        [token] = await session.list(OAuthToken)
        grant = await session.get(OAuthGrant, token.grant_id)
        assert grant is not None
        expired = datetime.now(UTC) - timedelta(seconds=1)
        if invalidity == "token-expired":
            token.access_expires_at = expired
        elif invalidity == "grant-expired":
            grant.expires_at = expired
        elif invalidity == "wrong-resource":
            app.auth.config.oauth_issuer_url = AnyHttpUrl("https://other.test")
        elif invalidity == "disabled":
            app.auth.config.oauth_issuer_url = None
        else:
            bearer = issued.json()["refresh_token"]
        await session.commit()
    headers = {"Authorization": f"Bearer {bearer}"}
    response = await browser.get("/test/hooks", headers=headers)
    assert response.status_code == 401
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        TestClient(app).websocket_connect("/api/acp", headers=headers),
    ):
        pytest.fail("An invalid credential opened ACP")
    assert denied.value.status_code == 401
