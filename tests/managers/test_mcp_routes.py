from collections.abc import AsyncIterator

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.schemas.user import User
from tests.agent.test_mcp import ENCRYPTION_KEY
from tests.support.users import auth_config

URL = "/api/mcp/instances"
REQUEST = {
    "name": "My search",
    "namespace": "search",
    "url": "https://mcp.example/mcp",
    "auth": {"kind": "bearer", "token": "provider-secret"},
}


@pytest.fixture
async def client(in_memory_engine: AsyncEngine) -> AsyncIterator[httpx2.AsyncClient]:
    app = Octomate(
        config=OctomateConfig(auth=auth_config()), oauth_encryption_key=ENCRYPTION_KEY
    )
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
