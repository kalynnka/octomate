"""The browser half of an authorization-code connection, driven as a browser does.

Everything here goes through the real router rather than the manager, because what
these routes owe the user is a redirect or a page — and what they owe everyone else
is to give nothing away when they refuse.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.managers.oauth import OAuthManager
from octomate.oauth.routes import oauth_manager, oauth_router
from octomate.schemas.user import UserProfile

from tests.managers.test_oauth import (
    LINEAR_CONNECTOR_ID,
    FakeAuthorizationCodeFlow,
    linear_manager,
    started,
)


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def browser(manager: OAuthManager) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(oauth_router)
    # The routes resolve their manager off the serving Octomate instance; a test
    # substitutes one rather than standing a whole application up around it.
    app.dependency_overrides[oauth_manager] = lambda: manager
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    )


async def linear_browser() -> tuple[
    OAuthManager, UserProfile, FakeAuthorizationCodeFlow, httpx.AsyncClient
]:
    manager, profile, flow = await linear_manager()
    return manager, profile, flow, browser(manager)


async def test_the_start_link_redirects_to_the_staged_provider_request() -> None:
    manager, profile, flow, client = await linear_browser()
    authorization, _ = await started(manager, profile, flow)

    async with client:
        response = await client.get(str(authorization.authorization_uri))

    assert response.status_code == 307
    # The provider request never reached the user until they opened the link.
    assert response.headers["location"] == (
        "https://example.com/authorize?state=provider-secret-state"
    )


async def test_an_unknown_start_link_says_only_that_it_is_finished() -> None:
    manager, _profile, _flow, client = await linear_browser()

    async with client:
        response = await client.get(
            "/oauth/linear/start/00000000-0000-7000-8000-000000000000"
        )

    assert response.status_code == 404
    assert "expired" in response.text
    # Someone who guessed a UUID learns nothing from having tried.
    assert "linear" not in response.text.lower()


async def test_the_callback_connects_and_says_who() -> None:
    manager, profile, flow, client = await linear_browser()
    _, state = await started(manager, profile, flow)

    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert response.status_code == 200
    assert "Connected as Alice" in response.text
    assert await manager.access_token(profile, LINEAR_CONNECTOR_ID) is not None


async def test_a_replayed_callback_gives_the_same_finished_page() -> None:
    manager, profile, flow, client = await linear_browser()
    _, state = await started(manager, profile, flow)

    async with client:
        await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )
        replay = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert replay.status_code == 404
    assert flow.exchanges == [("auth-code", "pkce-verifier")]


async def test_a_declined_authorization_closes_its_operation() -> None:
    manager, profile, flow, client = await linear_browser()
    _, state = await started(manager, profile, flow)

    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "error": "access_denied"},
        )
        # The link the user turned down stops working immediately.
        after = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert response.status_code == 200
    assert "declined" in response.text
    assert after.status_code == 404
    assert flow.exchanges == []


async def test_a_callback_without_an_authorization_is_refused() -> None:
    manager, _profile, _flow, client = await linear_browser()

    async with client:
        response = await client.get(f"/oauth/{LINEAR_CONNECTOR_ID}/callback")

    assert response.status_code == 400


async def test_a_failed_exchange_does_not_leak_the_provider_error() -> None:
    manager, profile, flow, client = await linear_browser()
    _, state = await started(manager, profile, flow)

    flow.exchange_refused = ValueError(
        "Linear authorization failed: client_secret is wrong"
    )

    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert response.status_code == 502
    assert "client_secret" not in response.text
