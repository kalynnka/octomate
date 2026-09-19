"""The browser half of an authorization-code connection, driven as a browser does.

Everything here goes through the real router rather than the manager, because what
these routes owe the user is a redirect or a page — and what they owe everyone else
is to give nothing away when they refuse.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.dependencies import oauth_manager
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.oauth.routes import oauth_router
from octomate.schemas.auth import LinkProfileSession
from octomate.schemas.oauth import OAuthGrant
from octomate.schemas.user import UserProfile
from tests.managers.test_oauth import (
    LINEAR_CONNECTOR_ID,
    FakeAuthorizationCodeFlow,
    linear_manager,
    started,
)
from tests.support.users import a_user


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


def browser(manager: OAuthManager) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(oauth_router)
    app.dependency_overrides[oauth_manager] = lambda: manager
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    )


async def linear_browser() -> tuple[
    OAuthManager, UserProfile, FakeAuthorizationCodeFlow, httpx.AsyncClient
]:
    manager, profile, flow = await linear_manager()
    manager.users.authorization_base_uri = AnyHttpUrl("https://octomate.example")
    return manager, profile, flow, browser(manager)


class ChannelOAuthConnector(OAuthConnector):
    async def resolve_profile(self, grant: OAuthGrant) -> UserProfile | None:
        if grant.subject is None:
            return None
        return UserProfile(
            channel_user_id=grant.subject, name=grant.account_label or ""
        )


async def channel_browser() -> tuple[
    OAuthManager, UserProfile, FakeAuthorizationCodeFlow, httpx.AsyncClient
]:
    manager, profile, flow, client = await linear_browser()
    connector = manager.connector(LINEAR_CONNECTOR_ID)
    manager.connectors[connector.id] = ChannelOAuthConnector(
        id=connector.id,
        flows=connector.flows,
        callback_transport=connector.callback_transport,
    )
    return manager, profile, flow, client


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
    _manager, _profile, _flow, client = await linear_browser()

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
    owner = await manager.users.owner(profile)
    assert owner is not None
    assert await manager.access_token(owner, LINEAR_CONNECTOR_ID) is not None


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
    _manager, _profile, _flow, client = await linear_browser()

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


@pytest.mark.parametrize("existing", [False, True])
async def test_callback_links_the_authorized_profile_to_the_initiating_account(
    existing: bool,
) -> None:
    manager, source, flow, client = await channel_browser()
    previous = (
        await manager.users.ensure_profile(
            LINEAR_CONNECTOR_ID, UserProfile(channel_user_id="usr_42", name="Old name")
        )
        if existing
        else None
    )
    if previous is not None:
        await manager.users.start_link_profile(previous)
    _, state = await started(manager, source, flow)

    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code", "user_id": "attacker"},
        )
        replay = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert response.status_code == 200
    assert "Your channel profile is linked" in response.text
    assert "location" not in response.headers
    owner = await manager.users.owner(source)
    assert owner is not None
    stored = await manager.users.profile(LINEAR_CONNECTOR_ID, "usr_42")
    assert stored is not None
    assert stored.channel_user_id == flow.grant.subject
    assert stored.id != source.id
    assert stored.name == "Alice"
    assert stored.user_id == owner.id
    if previous is not None:
        assert stored.id == previous.id
    assert await manager.access_token(owner, LINEAR_CONNECTOR_ID) is not None
    assert replay.status_code == 404
    async with async_session() as session:
        tickets = await session.list(LinkProfileSession)
    if previous is not None:
        [ticket] = tickets
        assert ticket.consumed_at is not None
    else:
        assert tickets == []


@pytest.mark.parametrize("other_owner", [False, True])
async def test_callback_preserves_existing_profile_ownership(
    other_owner: bool,
) -> None:
    manager, source, flow, client = await channel_browser()
    owner = await manager.users.owner(source)
    assert owner is not None
    linked_owner = await a_user("someone-else") if other_owner else owner
    await manager.users.ensure_profile(
        LINEAR_CONNECTOR_ID,
        UserProfile(channel_user_id="usr_42", user_id=linked_owner.id),
    )
    _, state = await started(manager, source, flow)

    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )

    assert response.status_code == 200
    assert "Connected as Alice" in response.text
    if other_owner:
        assert "profile linking could not be completed" in response.text
    else:
        assert "Your channel profile is linked" in response.text
    assert "location" not in response.headers
    stored = await manager.users.profile(LINEAR_CONNECTOR_ID, "usr_42")
    assert stored is not None
    assert stored.user_id == linked_owner.id
    async with async_session() as session:
        assert await session.list(LinkProfileSession) == []


@pytest.mark.parametrize("stage", ["resolve_profile", "link_verified_profile"])
async def test_optional_linking_failure_preserves_the_completed_connection(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, source, flow, client = await channel_browser()
    monkeypatch.setattr(
        type(manager.connector(LINEAR_CONNECTOR_ID))
        if stage == "resolve_profile"
        else type(manager.users),
        stage,
        AsyncMock(side_effect=ValueError("private-provider-detail")),
    )
    _, state = await started(manager, source, flow)
    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )
    assert response.status_code == 200
    assert "Connected as Alice" in response.text
    assert "profile linking could not be completed" in response.text
    assert "private-provider-detail" not in response.text
    owner = await manager.users.owner(source)
    assert owner is not None
    assert await manager.access_token(owner, LINEAR_CONNECTOR_ID) is not None


async def test_no_linking_config_skips_profile_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, source, flow, client = await channel_browser()
    manager.users.authorization_base_uri = None
    resolve = AsyncMock()
    monkeypatch.setattr(ChannelOAuthConnector, "resolve_profile", resolve)
    _, state = await started(manager, source, flow)
    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )
    assert response.status_code == 200
    assert "Connected as Alice" in response.text
    resolve.assert_not_awaited()


async def test_oauth_profile_resolution_cannot_assign_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, source, flow, client = await channel_browser()
    monkeypatch.setattr(
        ChannelOAuthConnector, "resolve_profile", AsyncMock(return_value=source)
    )
    _, state = await started(manager, source, flow)
    async with client:
        response = await client.get(
            f"/oauth/{LINEAR_CONNECTOR_ID}/callback",
            params={"state": state, "code": "auth-code"},
        )
    assert response.status_code == 200
    assert "profile linking could not be completed" in response.text
    assert (
        await manager.users.profile(LINEAR_CONNECTOR_ID, source.channel_user_id) is None
    )
