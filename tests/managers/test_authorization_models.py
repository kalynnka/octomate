"""Persistence boundaries for inbound OAuth, independent of protocol handlers."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.schemas.authorization import (
    OAuthAuthorization,
    OAuthClient,
    OAuthGrant,
    OAuthToken,
)
from octomate.schemas.user import User


@pytest.fixture(autouse=True)
async def authorization_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
async def grant() -> OAuthGrant:
    client = OAuthClient(
        name="Native harness",
        redirect_uris=["http://127.0.0.1:49152/callback"],
        scopes=["mcp"],
    )
    user = User(username="alice")
    async with async_session() as session:
        session.add(client)
        session.add(user)
        await session.commit()
        grant = OAuthGrant(
            client_id=client.id,
            user_id=user.id,
            resource="https://octomate.test/octomate/mcp",
            scopes=["mcp"],
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(grant)
        await session.commit()
    return grant


async def test_grants_are_independent_and_survive_new_sessions(
    grant: OAuthGrant,
) -> None:
    second = OAuthGrant(
        client_id=grant.client_id,
        user_id=grant.user_id,
        resource=grant.resource,
        scopes=grant.scopes,
        expires_at=grant.expires_at,
    )
    async with async_session() as session:
        session.add(second)
        first = await session.get(OAuthGrant, grant.id)
        assert first is not None
        first.revoked_at = datetime.now(UTC)
        await session.commit()
    async with async_session() as session:
        first = await session.get(OAuthGrant, grant.id)
        other = await session.get(OAuthGrant, second.id)
        client = await session.get(OAuthClient, grant.client_id)
    assert first is not None
    assert first.revoked_at is not None
    assert other is not None
    assert other.revoked_at is None
    assert other.expires_at == grant.expires_at
    assert client is not None
    assert client.redirect_uris == ["http://127.0.0.1:49152/callback"]


async def test_spent_refresh_evidence_survives_rotation(grant: OAuthGrant) -> None:
    token = OAuthToken(
        grant_id=grant.id,
        access_token_hash=SecretStr("a" * 64),
        refresh_token_hash=SecretStr("b" * 64),
        scopes=grant.scopes,
        access_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    async with async_session() as session:
        session.add(token)
        await session.commit()
    async with async_session() as session:
        spent = await session.get(OAuthToken, token.id)
        assert spent is not None
        spent.refresh_used_at = datetime.now(UTC)
        session.add(
            OAuthToken(
                grant_id=grant.id,
                access_token_hash=SecretStr("c" * 64),
                refresh_token_hash=SecretStr("d" * 64),
                scopes=grant.scopes,
                access_expires_at=token.access_expires_at,
            )
        )
        await session.commit()
    async with async_session() as session:
        spent = await session.one_or_none(
            OAuthToken,
            expressions=[OAuthToken["refresh_token_hash"] == token.refresh_token_hash],
        )
        generations = list(await session.list(OAuthToken, limit=None))
    assert spent is not None
    assert spent.refresh_used_at is not None
    assert len(generations) == 2
    for generation in generations:
        for secret in (generation.access_token_hash, generation.refresh_token_hash):
            assert secret.get_secret_value() not in str(generation)
            assert secret.get_secret_value() not in repr(generation)
            assert secret.get_secret_value() not in generation.model_dump_json()


@pytest.mark.parametrize("mismatch", ["client", "missing_code"])
async def test_authorization_cannot_attach_an_unrelated_or_incomplete_grant(
    grant: OAuthGrant, mismatch: str
) -> None:
    client_id = grant.client_id
    if mismatch == "client":
        client = OAuthClient(
            name="Other", redirect_uris=["http://localhost/cb"], scopes=["mcp"]
        )
        async with async_session() as session:
            session.add(client)
            await session.commit()
        client_id = client.id
    request = OAuthAuthorization(
        client_id=client_id,
        ticket_hash=SecretStr("e" * 64),
        redirect_uri="http://localhost/cb",
        redirect_uri_provided_explicitly=True,
        resource=grant.resource,
        scopes=grant.scopes,
        code_challenge="A" * 43,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        grant_id=grant.id,
        code_hash=SecretStr("f" * 64) if mismatch == "client" else None,
    )
    async with async_session() as session:
        session.add(request)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_token_requires_an_existing_grant() -> None:
    async with async_session() as session:
        session.add(
            OAuthToken(
                grant_id=uuid7(),
                access_token_hash=SecretStr("a" * 64),
                refresh_token_hash=SecretStr("b" * 64),
                scopes=["mcp"],
                access_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_concurrent_grant_writes_reject_stale_state(grant: OAuthGrant) -> None:
    async with async_session() as first, async_session() as second:
        one = await first.get(OAuthGrant, grant.id)
        two = await second.get(OAuthGrant, grant.id)
        assert one is not None
        assert two is not None
        one.revoked_at = datetime.now(UTC)
        await first.commit()
        two.expires_at += timedelta(days=1)
        with pytest.raises(StaleDataError):
            await second.commit()
