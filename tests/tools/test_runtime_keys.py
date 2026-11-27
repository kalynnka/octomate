"""Agent runtime credentials reuse the existing scoped API-key lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.base import OctomateConfig
from octomate.database import async_session
from octomate.schemas.auth import RuntimeApiKey
from octomate.tentacles.inkling import InklingTentacle
from tests.support.users import a_user, auth_config, runtime_api_keys


async def test_inkling_reuses_one_scoped_key_per_user_across_exit(
    in_memory_engine: AsyncEngine,
) -> None:
    alice = await a_user("alice")
    bob = await a_user("bob")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        habitats=host.habitats["native"],
        models={"test": TestModel()},
    )
    assert host.auth is not None

    async with agent:
        first, repeated, other = await asyncio.gather(
            agent.runtime_api_key(alice.id),
            agent.runtime_api_key(alice.id),
            agent.runtime_api_key(bob.id),
        )
        assert first is not None
        assert other is not None
        assert repeated is not None
        assert repeated.bearer == first.bearer
        assert first.bearer != other.bearer
        assert len(await runtime_api_keys(alice.id)) == 1
        assert len(await runtime_api_keys(bob.id)) == 1
        for issued, owner in ((first, alice), (other, bob)):
            assert issued.bearer is not None
            authenticated = await host.auth.authenticate_api_key(
                issued.bearer, scope="mcp"
            )
            assert authenticated is not None
            assert authenticated.user_id == owner.id
            hook_key = await host.auth.authenticate_api_key(
                issued.bearer, scope="hooks"
            )
            assert hook_key is not None
            assert hook_key.id == authenticated.id
            assert (
                await host.auth.authenticate_api_key(issued.bearer, scope="acp") is None
            )

    for issued in (first, other):
        assert issued.bearer is not None
        assert (
            await host.auth.authenticate_api_key(issued.bearer, scope="mcp") is not None
        )
        assert (
            await host.auth.authenticate_api_key(issued.bearer, scope="hooks")
            is not None
        )


async def test_inkling_refreshes_expired_credentials_once(
    in_memory_engine: AsyncEngine,
) -> None:
    user = await a_user()
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        habitats=host.habitats["native"],
        models={"test": TestModel()},
    )
    assert host.auth is not None

    async with agent:
        expired = await agent.runtime_api_key(user.id)
        assert expired is not None
        assert expired.bearer is not None
        async with async_session() as session:
            stored = await session.get(RuntimeApiKey, expired.id)
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        current, repeated = await asyncio.gather(
            agent.runtime_api_key(user.id), agent.runtime_api_key(user.id)
        )

        assert current is not None
        assert current.bearer is not None
        assert repeated is not None
        assert repeated.bearer == current.bearer
        assert current.bearer != expired.bearer
        assert await host.auth.authenticate_api_key(expired.bearer, scope="mcp") is None
        assert (
            await host.auth.authenticate_api_key(current.bearer, scope="mcp")
            is not None
        )


async def test_runtime_keys_require_a_registered_identity_and_configured_auth() -> None:
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    assert host.auth is not None
    agent = InklingTentacle(
        "inkling",
        host,
        auth=None,
        habitats=host.habitats["native"],
        models={"test": TestModel()},
    )
    assert agent.auth is None
    assert await agent.runtime_api_key(None) is None
    assert await agent.runtime_api_key(uuid7()) is None
