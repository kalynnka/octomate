"""Agent MCP credentials reuse the existing scoped API-key lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.base import OctomateConfig
from octomate.tentacles.inkling import InklingTentacle
from tests.support.users import a_user, auth_config


async def test_inkling_reuses_one_scoped_key_per_user_and_revokes_on_exit(
    in_memory_engine: AsyncEngine,
) -> None:
    alice = await a_user("alice")
    bob = await a_user("bob")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        sandboxes=host.sandboxes,
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
        assert repeated is first
        assert first.token != other.token
        assert len(await host.auth.list_api_keys(alice.id)) == 1
        assert len(await host.auth.list_api_keys(bob.id)) == 1
        for issued, owner in ((first, alice), (other, bob)):
            authenticated = await host.auth.authenticate_api_key(
                issued.token, scope="mcp"
            )
            assert authenticated is not None
            assert authenticated.user_id == owner.id
            assert (
                await host.auth.authenticate_api_key(issued.token, scope="hooks")
                is None
            )

    assert not agent.api_keys
    for issued in (first, other):
        assert await host.auth.authenticate_api_key(issued.token, scope="mcp") is None


async def test_inkling_refreshes_expired_credentials_once(
    in_memory_engine: AsyncEngine,
) -> None:
    user = await a_user()
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        sandboxes=host.sandboxes,
        models={"test": TestModel()},
    )
    assert host.auth is not None

    async with agent:
        expired = await agent.runtime_api_key(user.id)
        assert expired is not None
        expired.key.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        current, repeated = await asyncio.gather(
            agent.runtime_api_key(user.id), agent.runtime_api_key(user.id)
        )

        assert current is not None
        assert repeated is current
        assert current.token != expired.token
        assert await host.auth.authenticate_api_key(expired.token, scope="mcp") is None
        assert (
            await host.auth.authenticate_api_key(current.token, scope="mcp") is not None
        )


async def test_runtime_keys_require_a_registered_identity_and_configured_auth() -> None:
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    assert host.auth is not None
    agent = InklingTentacle(
        "inkling",
        host,
        auth=None,
        sandboxes=host.sandboxes,
        models={"test": TestModel()},
    )
    assert agent.auth is None
    assert await agent.runtime_api_key(None) is None
    assert await agent.runtime_api_key(uuid7()) is None
    assert not agent.api_keys
