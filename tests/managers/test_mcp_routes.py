from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from pydantic import AnyUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.managers.oauth import OAuthConnector, UnusableOAuthOperation
from octomate.schemas.mcp import OAuthMcp
from octomate.schemas.oauth import OAuthConnection, OAuthOperation
from octomate.schemas.user import User, UserProfile
from octomate.types.oauth import OAuthFlowKind
from tests.agent.test_mcp import ENCRYPTION_KEY
from tests.managers.test_oauth import (
    FakeAuthorizationCodeFlow,
    FakeDeviceFlow,
    direct_http,
)
from tests.support.users import auth_config

URL = "/api/mcp"
REQUEST = {
    "name": "My search",
    "namespace": "search",
    "url": "https://mcp.example/mcp",
    "auth": {"kind": "bearer", "token": "provider-secret"},
}


@pytest.fixture
def app() -> Octomate:
    return Octomate(
        config=OctomateConfig(auth=auth_config()), oauth_encryption_key=ENCRYPTION_KEY
    )


@pytest.fixture
async def client(
    in_memory_engine: AsyncEngine, app: Octomate
) -> AsyncIterator[httpx2.AsyncClient]:
    assert app.auth is not None
    async with async_session() as session:
        for username in ("alice", "bob"):
            session.add(
                User(
                    username=username,
                    password_hash=await app.auth.hash_password(
                        SecretStr("correct horse battery staple")
                    ),
                )
            )
        await session.commit()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://octomate",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        assert response.status_code == 204
        yield client


@pytest.fixture
async def oauth_mcp(client: httpx2.AsyncClient, app: Octomate) -> OAuthMcp:
    app.oauth.register(
        OAuthConnector(
            id="github",
            flows=[FakeDeviceFlow(), FakeAuthorizationCodeFlow()],
            callback_transport=direct_http(),
            mcp_url=AnyUrl("https://mcp.example/mcp"),
        )
    )
    async with async_session() as session:
        user = await session.one_or_none(
            User, expressions=[User["username"] == "alice"]
        )
        assert user is not None
        mcp = OAuthMcp(
            user_id=user.id,
            name="GitHub",
            namespace="personal/github",
            url="https://mcp.example/mcp",
            tentacle_id="github",
        )
        session.add(mcp)
        await session.commit()
    return mcp


@pytest.mark.parametrize("flow", ["device", "authorization_code"])
async def test_pending_authorization_restores_the_same_attempt(
    client: httpx2.AsyncClient, oauth_mcp: OAuthMcp, flow: OAuthFlowKind
) -> None:
    path = f"{URL}/{oauth_mcp.id}"
    started = await client.post(f"{path}/connect", params={"flow": flow})
    assert started.status_code == 200
    for _ in range(2):
        restored = await client.get(f"{path}/authorization")
        assert restored.status_code == 200
        assert restored.headers["cache-control"] == "no-store"
        assert restored.json() == started.json()
        for secret in ("device-secret", "pkce-verifier", "provider-secret-state"):
            assert secret not in restored.text
    if flow == "device":
        assert restored.json()["user_code"] == "ABCD-EFGH"
    async with async_session() as session:
        assert len(await session.list(OAuthOperation, limit=None)) == 1


@pytest.mark.parametrize("flow", ["device", "authorization_code"])
async def test_cancelling_authorization_is_private_and_keeps_the_installation_and_grant(
    client: httpx2.AsyncClient, app: Octomate, oauth_mcp: OAuthMcp, flow: OAuthFlowKind
) -> None:
    path = f"{URL}/{oauth_mcp.id}"
    await client.post(f"{path}/connect", params={"flow": flow})
    async with async_session() as session:
        session.add(
            OAuthConnection(
                user_id=oauth_mcp.user_id,
                mcp_id=oauth_mcp.id,
                connector_id="github",
                encrypted_tokens=b"existing encrypted grant",
            )
        )
        other = OAuthMcp(
            user_id=oauth_mcp.user_id,
            name="Other workspace",
            namespace="personal/other",
            url=oauth_mcp.url,
            tentacle_id="github",
        )
        session.add(other)
        await session.commit()
    await client.post(f"{URL}/{other.id}/connect", params={"flow": flow})

    await client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "correct horse battery staple"},
    )
    assert (await client.get(f"{path}/authorization")).status_code == 404
    assert (await client.delete(f"{path}/authorization")).status_code == 404
    await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
    )
    del client.headers["X-Octomate-Request"]
    assert (await client.delete(f"{path}/authorization")).status_code == 403
    client.headers["X-Octomate-Request"] = "1"
    assert (await client.delete(f"{path}/authorization")).status_code == 204
    assert (await client.delete(f"{path}/authorization")).status_code == 204
    assert (await client.get(f"{path}/authorization")).json() is None
    assert (await client.get(f"{URL}/{other.id}/authorization")).json() is not None
    async with async_session() as session:
        kept = await session.get(OAuthMcp, oauth_mcp.id)
        assert kept is not None
        assert kept.enabled
        [grant] = await session.list(OAuthConnection, limit=None)
        assert grant.encrypted_tokens == b"existing encrypted grant"
        assert grant.status == "active"
        [operation] = await session.list(
            OAuthOperation, expressions=[OAuthOperation["mcp_id"] == oauth_mcp.id]
        )
        assert operation.consumed_at is not None
        user = await session.get(User, oauth_mcp.user_id)
        assert user is not None
    if flow == "device":
        with pytest.raises(ValueError, match="already been consumed"):
            await app.oauth.complete(user, operation.id)
    else:
        with pytest.raises(UnusableOAuthOperation, match="already been consumed"):
            await app.oauth.staged_authorization("github", operation.id)
    assert (
        await client.post(f"{path}/connect", params={"flow": flow})
    ).status_code == 200
    assert (await client.get(f"{path}/authorization")).json()["operation_id"] != str(
        operation.id
    )


@pytest.mark.parametrize("flow", ["device", "authorization_code"])
@pytest.mark.parametrize("expired", [True, False])
async def test_finished_authorization_is_not_restored(
    client: httpx2.AsyncClient, oauth_mcp: OAuthMcp, flow: OAuthFlowKind, expired: bool
) -> None:
    path = f"{URL}/{oauth_mcp.id}"
    await client.post(f"{path}/connect", params={"flow": flow})
    async with async_session() as session:
        [operation] = await session.list(OAuthOperation, limit=None)
        if expired:
            operation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            operation.consumed_at = datetime.now(UTC)
        await session.commit()
    response = await client.get(f"{path}/authorization")
    assert response.status_code == 200
    assert response.json() is None


async def test_browser_can_resume_and_confirm_its_users_channel_started_device_flow(
    client: httpx2.AsyncClient, app: Octomate, oauth_mcp: OAuthMcp
) -> None:
    async with async_session() as session:
        user = await session.get(User, oauth_mcp.user_id)
        assert user is not None
        profile = UserProfile(
            user_id=user.id, channel_tentacle_id="slack", channel_user_id="U1"
        )
        session.add(profile)
        await session.commit()
    started = await app.mcp.connect(user, oauth_mcp.id, profile=profile, flow="device")
    path = f"{URL}/{oauth_mcp.id}"
    restored = await client.get(f"{path}/authorization")
    assert restored.status_code == 200
    assert restored.json()["operation_id"] == str(started.operation_id)
    assert restored.json()["user_code"] == "ABCD-EFGH"
    confirmed = await client.post(f"{path}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "active"}
    assert (await client.get(f"{path}/authorization")).json() is None


async def test_management_requires_login_and_browser_write_header(
    client: httpx2.AsyncClient,
) -> None:
    del client.headers["X-Octomate-Request"]
    assert (await client.post(URL, json=REQUEST)).status_code == 403
    client.cookies.clear()
    assert (await client.get(URL)).status_code == 401
    client.headers["X-Octomate-Request"] = "1"
    assert (await client.post(URL, json=REQUEST)).status_code == 401


async def test_management_lifecycle_is_private_and_never_returns_credentials(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.post(URL, json=REQUEST)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    mcp_id = response.json()["id"]
    assert response.json()["auth_kind"] == "bearer"
    assert response.json()["namespace"] == "personal/search"
    for secret in ("provider-secret", "encrypted_token", "user_id", '"token"'):
        assert secret not in response.text
    assert len((await client.get(URL)).json()) == 1
    assert (await client.post(URL, json=REQUEST)).status_code == 400
    disabled = await client.post(f"{URL}/{mcp_id}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = await client.post(f"{URL}/{mcp_id}/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    await client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "correct horse battery staple"},
    )
    assert (await client.get(URL)).json() == []
    assert (await client.post(f"{URL}/{mcp_id}/disable")).status_code == 404
    assert (await client.post(f"{URL}/{mcp_id}/enable")).status_code == 404
    assert (await client.delete(f"{URL}/{mcp_id}")).status_code == 404
    assert (await client.post(URL, json=REQUEST)).status_code == 201

    await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
    )
    assert (await client.delete(f"{URL}/{mcp_id}")).status_code == 204
    assert (await client.get(URL)).json() == []


async def test_validation_does_not_echo_credentials(client: httpx2.AsyncClient) -> None:
    response = await client.post(
        URL,
        json={
            **REQUEST,
            "url": "https://user:password@example.com/mcp",
            "auth": {"kind": "unknown", "token": "provider-secret"},
        },
    )
    assert response.status_code == 422
    assert "provider-secret" not in response.text
    assert "user:password" not in response.text


async def test_oauth_cannot_silently_create_an_unauthenticated_instance(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.post(URL, json={**REQUEST, "auth": {"kind": "oauth"}})
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Configure oauth.callback_base_uri before installing dynamic OAuth MCPs"
    )
    assert (await client.get(URL)).json() == []
