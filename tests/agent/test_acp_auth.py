"""ACP authenticates at the handshake, before an adapter can be started."""

from contextvars import Context
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, HTTPException, WebSocket
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.schemas.auth import UserApiKey
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import Thread
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.acp.auth import acp_sender
from octomate.tentacles.acp.client import AcpProvider
from octomate.tentacles.hooks import hook_guard
from octomate.types.auth import ApiKeyScope
from tests.support.users import a_user, auth_config


@pytest.fixture
def app(in_memory_engine: AsyncEngine) -> Octomate:
    app = Octomate(config=OctomateConfig(auth=auth_config()))

    @app.websocket("/test/acp/{provider}")
    async def connection(
        websocket: WebSocket,
        sender: Annotated[UserProfile, Depends(acp_sender)],
    ) -> None:
        await websocket.accept()
        await websocket.send_json(
            {
                "user_id": str(sender.user_id),
                "username": sender.channel_user_id,
                "runtime": sender.channel_tentacle_id,
            }
        )
        await websocket.close()

    return app


@pytest.fixture
async def user(app: Octomate) -> User:
    return await a_user("alice")


@pytest.mark.parametrize("provider", list(AcpProvider))
@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
async def test_scoped_key_resolves_its_owner_without_inherited_database_context(
    app: Octomate, user: User, provider: AcpProvider, scheme: str
) -> None:
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])

    def connect() -> None:
        with TestClient(app).websocket_connect(
            f"/test/acp/{provider}?username=bob",
            headers={"Authorization": f"{scheme} {key.token.get_secret_value()}"},
        ) as socket:
            assert socket.receive_json() == {
                "user_id": str(user.id),
                "username": "alice",
                "runtime": provider.native_id,
            }

    Context().run(connect)
    async with async_session() as session:
        assert not await session.list(UserProfile)
        assert not await session.list(Thread)
        assert not await session.list(Conversation)


@pytest.mark.parametrize("scopes", [["hooks"], ["mcp"], ["hooks", "mcp"]])
async def test_existing_scopes_cannot_authorize_acp(
    app: Octomate, user: User, scopes: list[ApiKeyScope]
) -> None:
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="Existing client", scopes=scopes)
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        TestClient(app).websocket_connect(
            "/test/acp/claude",
            headers={"Authorization": f"Bearer {key.token.get_secret_value()}"},
        ),
    ):
        pytest.fail("A key without the ACP scope reached the endpoint")
    assert denial.value.status_code == 401
    assert denial.value.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization", [None, "Bearer unknown", "token", "Basic token", "Bearer "]
)
def test_missing_or_invalid_credentials_are_refused(
    app: Octomate, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        TestClient(app).websocket_connect("/test/acp/codex", headers=headers),
    ):
        pytest.fail("Invalid credentials reached the endpoint")
    assert denial.value.status_code == 401


@pytest.mark.parametrize("state", ["revoked", "expired"])
async def test_revoked_or_expired_keys_are_refused(
    app: Octomate, user: User, state: str
) -> None:
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])
    if state == "revoked":
        await app.auth.revoke_api_key(user.id, key.key.id)
    else:
        async with async_session() as session:
            stored = await session.get(UserApiKey, key.key.id)
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        TestClient(app).websocket_connect(
            "/test/acp/claude",
            headers={"Authorization": f"Bearer {key.token.get_secret_value()}"},
        ),
    ):
        pytest.fail("An inactive key reached the endpoint")
    assert denial.value.status_code == 401


async def test_browser_session_and_query_token_cannot_authorize_execution(
    app: Octomate,
) -> None:
    assert app.auth is not None
    password = SecretStr("Correct horse battery staple1!")
    user = await app.auth.register(
        "alice", password, await app.auth.invite(), name="Alice"
    )
    tokens = await app.auth.login(user.username, password)
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])
    client = TestClient(app)
    client.cookies.set("octomate_access", tokens.access_token.get_secret_value())
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        client.websocket_connect(
            f"/test/acp/claude?token={key.token.get_secret_value()}"
        ),
    ):
        pytest.fail("Cookies or a query token authorized execution")
    assert denial.value.status_code == 401


async def test_acp_scope_does_not_grant_hooks_or_mcp(app: Octomate, user: User) -> None:
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])
    token = key.token.get_secret_value()
    assert await app.bearers.verify_token(token) is None
    with pytest.raises(HTTPException) as denial:
        await hook_guard(app.bearers)(f"Bearer {token}")
    assert denial.value.status_code == 401


async def test_authentication_must_be_configured(app: Octomate) -> None:
    app.bearers.auth = None
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        TestClient(app).websocket_connect(
            "/test/acp/claude", headers={"Authorization": "Bearer token"}
        ),
    ):
        pytest.fail("An unconfigured verifier authorized execution")
    assert denial.value.status_code == 401
