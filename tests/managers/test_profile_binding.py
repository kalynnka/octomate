from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import AuthConfig, OAuthConfig, OctomateConfig
from octomate.database import AsyncSession, async_session
from octomate.managers.user import (
    InvalidLinkProfile,
    LinkProfileUnavailable,
    ProfileAlreadyLinked,
    UserManager,
)
from octomate.schemas.auth import LinkProfileSession
from octomate.schemas.user import User, UserProfile

PASSWORD = SecretStr("Correct horse battery staple1!")


@pytest.fixture(autouse=True)
async def link_profile_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def manager() -> UserManager:
    return UserManager(
        authorization_base_uri=AnyHttpUrl("https://octomate.example"),
    )


async def account_and_profile() -> tuple[User, UserProfile]:
    user = User(username="alice", name="Alice")
    profile = UserProfile(
        channel_tentacle_id="slack",
        channel_user_id="U1",
        name="Alice on Slack",
    )
    async with async_session() as session:
        session.add(user)
        session.add(profile)
        await session.commit()
    return user, profile


def ticket_from(uri: AnyHttpUrl) -> SecretStr:
    [ticket] = parse_qs(urlsplit(str(uri)).fragment)["link-profile"]
    return SecretStr(ticket)


async def test_ticket_links_its_exact_profile_once(
    manager: UserManager,
) -> None:
    user, profile = await account_and_profile()

    authorization = await manager.start_link_profile(profile)
    ticket = ticket_from(authorization.authorization_uri)
    pending = await manager.inspect_link_profile(ticket)
    linked = await manager.confirm_link_profile(ticket, user)

    assert pending.profile.id == profile.id
    assert pending.profile.channel_user_id == "U1"
    assert linked.id == profile.id
    assert linked.user_id == user.id
    with pytest.raises(InvalidLinkProfile):
        await manager.confirm_link_profile(ticket, user)

    async with async_session() as session:
        [stored] = await session.list(LinkProfileSession)
        linked = await session.get(UserProfile, profile.id)
    assert stored.consumed_at is not None
    assert ticket.get_secret_value() not in stored.token_hash.get_secret_value()
    assert linked is not None
    assert linked.user_id == user.id


async def test_start_replaces_an_earlier_ticket(
    manager: UserManager,
) -> None:
    _, profile = await account_and_profile()
    first = ticket_from((await manager.start_link_profile(profile)).authorization_uri)
    second = ticket_from((await manager.start_link_profile(profile)).authorization_uri)

    assert first != second
    with pytest.raises(InvalidLinkProfile):
        await manager.inspect_link_profile(first)
    assert (await manager.inspect_link_profile(second)).profile.id == profile.id
    async with async_session() as session:
        assert len(await session.list(LinkProfileSession)) == 1


async def test_expired_and_owned_profiles_cannot_link(
    manager: UserManager,
) -> None:
    user, profile = await account_and_profile()
    ticket = ticket_from((await manager.start_link_profile(profile)).authorization_uri)
    async with async_session() as session:
        [stored] = await session.list(LinkProfileSession)
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    with pytest.raises(InvalidLinkProfile):
        await manager.inspect_link_profile(ticket)

    async with async_session() as session:
        stored_profile = await session.get(UserProfile, profile.id)
        assert stored_profile is not None
        stored_profile.user_id = user.id
        await session.commit()
    with pytest.raises(ProfileAlreadyLinked):
        await manager.start_link_profile(profile)


async def test_concurrent_confirmation_links_the_profile_once(
    manager: UserManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice, profile = await account_and_profile()
    bob = User(username="bob", name="Bob")
    async with async_session() as session:
        session.add(bob)
        await session.commit()
    ticket = ticket_from((await manager.start_link_profile(profile)).authorization_uri)
    other_process = UserManager(
        authorization_base_uri=manager.authorization_base_uri,
        authorization_lifetime=manager.authorization_lifetime,
    )
    barrier = asyncio.Barrier(2)
    flush = AsyncSession.flush

    async def simultaneous_flush(session: AsyncSession) -> None:
        await barrier.wait()
        await flush(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", simultaneous_flush)
        results = await asyncio.wait_for(
            asyncio.gather(
                manager.confirm_link_profile(ticket, alice),
                other_process.confirm_link_profile(ticket, bob),
                return_exceptions=True,
            ),
            timeout=10,
        )

    assert sum(isinstance(result, UserProfile) for result in results) == 1
    assert sum(isinstance(result, InvalidLinkProfile) for result in results) == 1
    async with async_session() as session:
        stored = await session.get(UserProfile, profile.id)
    assert stored is not None
    assert stored.user_id in {alice.id, bob.id}


@pytest.mark.parametrize("switch_account", [False, True])
async def test_browser_confirmation_uses_the_displayed_account(
    in_memory_engine: AsyncEngine,
    switch_account: bool,
) -> None:
    config = AuthConfig(
        access_token_salt=SecretStr("access-token-test-salt"),
        refresh_token_salt=SecretStr("refresh-token-test-salt"),
        api_key_salt=SecretStr("api-key-test-salt"),
    )
    users = UserManager()
    app = Octomate(
        config=OctomateConfig(
            auth=config,
            oauth=OAuthConfig(
                callback_base_uri=AnyHttpUrl("https://testserver"),
                authorization_lifetime=timedelta(minutes=3),
            ),
        ),
        users=users,
    )
    assert app.auth is not None
    user = User(
        username="alice",
        name="Alice",
        password_hash=await app.auth.hash_password(PASSWORD),
    )
    other = User(username="bob", password_hash=user.password_hash)
    profile = UserProfile(
        channel_tentacle_id="slack", channel_user_id="U1", name="Alice on Slack"
    )
    async with async_session() as session:
        session.add(user)
        session.add(other)
        session.add(profile)
        await session.commit()
    authorization = await users.start_link_profile(profile)
    assert str(authorization.authorization_uri).startswith("https://testserver/")
    assert (
        timedelta(minutes=2)
        < authorization.expires_at - datetime.now(UTC)
        <= timedelta(minutes=3)
    )
    ticket = ticket_from(authorization.authorization_uri)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        anonymous = await client.post(
            "/api/auth/link-profile/confirm",
            json={"token": ticket.get_secret_value()},
        )
        assert anonymous.status_code == 401

        login = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": PASSWORD.get_secret_value()},
        )
        assert login.status_code == 204
        inspected = await client.post(
            "/api/auth/link-profile/inspect",
            json={"token": ticket.get_secret_value()},
        )
        missing_account = await client.post(
            "/api/auth/link-profile/confirm",
            json={"token": ticket.get_secret_value()},
        )
        assert missing_account.status_code == 422

        if switch_account:
            await client.post("/api/auth/logout")
            login = await client.post(
                "/api/auth/login",
                json={"username": "bob", "password": PASSWORD.get_secret_value()},
            )
            assert login.status_code == 204
            refused = await client.post(
                "/api/auth/link-profile/confirm",
                json={
                    "token": ticket.get_secret_value(),
                    "expected_user_id": str(user.id),
                },
            )
            assert refused.status_code == 409
            assert "account changed" in refused.json()["detail"]
            assert (
                await app.users.inspect_link_profile(ticket)
            ).profile.user_id is None
            await client.post("/api/auth/logout")
            login = await client.post(
                "/api/auth/login",
                json={"username": "alice", "password": PASSWORD.get_secret_value()},
            )
            assert login.status_code == 204

        confirmed = await client.post(
            "/api/auth/link-profile/confirm",
            json={
                "token": ticket.get_secret_value(),
                "expected_user_id": str(user.id),
            },
        )

    assert inspected.status_code == 200
    assert inspected.json()["profile"]["id"] == str(profile.id)
    assert confirmed.status_code == 200
    assert confirmed.json()["user_id"] == str(user.id)


@pytest.mark.parametrize("configured", ["auth", "origin"])
async def test_link_profile_requires_both_local_auth_and_a_public_origin(
    configured: str,
) -> None:
    app = Octomate(
        config=OctomateConfig(
            auth=AuthConfig(
                access_token_salt=SecretStr("access-token-test-salt"),
                refresh_token_salt=SecretStr("refresh-token-test-salt"),
                api_key_salt=SecretStr("api-key-test-salt"),
            )
            if configured == "auth"
            else None,
            oauth=OAuthConfig(
                callback_base_uri=AnyHttpUrl("https://octomate.example")
                if configured == "origin"
                else None
            ),
        )
    )
    _, profile = await account_and_profile()
    with pytest.raises(
        LinkProfileUnavailable, match=r"local auth and oauth\.callback_base_uri"
    ):
        await app.users.start_link_profile(profile)
    async with async_session() as session:
        assert await session.list(LinkProfileSession) == []
