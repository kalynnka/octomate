"""The deployment's bearer registry: one set of credentials, every surface.

`KnownBearers` is what the served MCP endpoints verify tokens through and what
the hook routers guard with, so these tests pin the mapping — each user secret
to its user, anything else to nothing — and that both surfaces read it the
same way.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.config.users import UserConfig
from octomate.managers.user import UserManager
from octomate.mcp.base import KnownBearers
from octomate.tentacles.agents.hooks import hook_guard, hook_sender
from tests.support.config import registered


def a_registry() -> KnownBearers:
    return KnownBearers(
        {
            "lu": UserConfig.model_validate({"secret": "lu-token"}),
            "visitor": UserConfig(),
        }
    )


def test_owner_names_each_user() -> None:
    bearers = a_registry()

    assert bearers.owner("lu-token") == "lu"
    assert bearers.owner("stranger") is None
    # A user without a secret holds no bearer at all.
    assert "visitor" not in bearers.users


async def test_verify_token_carries_the_user_as_client_id() -> None:
    bearers = a_registry()

    user = await bearers.verify_token("lu-token")

    assert user is not None
    assert user.client_id == "lu"
    assert await bearers.verify_token("stranger") is None


def test_octomate_builds_bearers_from_its_config() -> None:
    octomate = Octomate(config=registered("lu-token"))

    assert octomate.bearers.owner("lu-token") == "lu"
    # No config, no users: the registry exists and rejects everything.
    assert Octomate().bearers.users == {}


async def test_hook_guard_yields_the_bearers_owner() -> None:
    """The dependency's value is the ledger's principal: whose token
    authenticated is who the request's rows are attributed to."""
    verify = hook_guard(a_registry(), "claude")

    assert await verify("Bearer lu-token") == "lu"


async def test_hook_guard_rejects_everything_else() -> None:
    verify = hook_guard(a_registry(), "claude")

    for authorization in (None, "Bearer stranger", "lu-token", "Basic lu-token"):
        with pytest.raises(HTTPException) as denial:
            await verify(authorization)
        assert denial.value.status_code == 401


def test_hook_guard_demands_a_registered_user() -> None:
    with pytest.raises(RuntimeError, match="no registered user carries a secret"):
        hook_guard(KnownBearers({}), "claude")


async def test_hook_sender_demands_a_registered_username(
    in_memory_engine: AsyncEngine,
) -> None:
    """The dependency trusts the guard and says so when that trust breaks: a
    verified username the registry never reconciled is a wiring bug worth an
    error, never a row written to nobody."""
    resolve = hook_sender(
        UserManager(), "claude-native", hook_guard(a_registry(), "claude")
    )

    with pytest.raises(RuntimeError, match="registry holds no such user"):
        await resolve("lu")
