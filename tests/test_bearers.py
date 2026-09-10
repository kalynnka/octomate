"""Scoped API tokens authenticate MCP and native hook requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.managers.auth import AuthManager
from octomate.managers.user import UserManager
from octomate.mcp.base import KnownBearers
from octomate.schemas.auth import UserApiKey
from octomate.tentacles.hooks import hook_guard, hook_sender
from tests.support.users import a_api_key, a_user, auth_config


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def auth() -> AuthManager:
    return AuthManager(auth_config())


async def test_owner_names_the_token_owner(auth: AuthManager) -> None:
    user = await a_user()
    await a_api_key(user, "omk_lu-token")
    bearers = KnownBearers(auth)

    assert await bearers.owner("omk_lu-token", scope="hooks") == "lu"
    assert await bearers.owner("stranger", scope="hooks") is None
    assert await bearers.owner("", scope="hooks") is None


async def test_scopes_separate_hooks_and_mcp(auth: AuthManager) -> None:
    user = await a_user()
    hook = await auth.create_api_key(user.id, name="hooks", scopes=["hooks"])
    mcp = await auth.create_api_key(user.id, name="editor", scopes=["mcp"])
    bearers = KnownBearers(auth)
    verify = hook_guard(bearers)

    assert await verify(f"Bearer {hook.token.get_secret_value()}") == "lu"
    assert await bearers.verify_token(hook.token.get_secret_value()) is None
    with pytest.raises(HTTPException) as denial:
        await verify(f"Bearer {mcp.token.get_secret_value()}")
    assert denial.value.status_code == 401
    principal = await bearers.verify_token(mcp.token.get_secret_value())
    assert principal is not None
    assert principal.client_id == "lu"
    assert principal.scopes == ["mcp"]


async def test_octomate_uses_its_auth_manager() -> None:
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    assert octomate.bearers.auth is octomate.auth
    user = await a_user()
    await a_api_key(user, "omk_lu-token")

    assert await octomate.bearers.owner("omk_lu-token", scope="hooks") == "lu"
    assert await Octomate().bearers.owner("omk_lu-token", scope="hooks") is None


async def test_revocation_applies_to_both_surfaces(auth: AuthManager) -> None:
    user = await a_user()
    key = await auth.create_api_key(user.id, name="laptop", scopes=["hooks", "mcp"])
    other = await auth.create_api_key(user.id, name="desktop", scopes=["hooks", "mcp"])
    token = key.token.get_secret_value()
    bearers = KnownBearers(auth)
    verify = hook_guard(bearers)
    assert await verify(f"Bearer {token}") == "lu"
    assert await bearers.verify_token(token) is not None

    await auth.revoke_api_key(user.id, key.key.id)

    assert await bearers.verify_token(token) is None
    with pytest.raises(HTTPException):
        await verify(f"Bearer {token}")
    assert await bearers.verify_token(other.token.get_secret_value()) is not None


async def test_expired_keys_are_rejected(auth: AuthManager) -> None:
    user = await a_user()
    key = await a_api_key(user, "omk_expired")
    async with async_session() as session:
        stored = await session.get(UserApiKey, key.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    bearers = KnownBearers(auth)
    assert await bearers.verify_token("omk_expired") is None
    with pytest.raises(HTTPException):
        await hook_guard(bearers)("Bearer omk_expired")


async def test_password_session_tokens_are_not_api_tokens(auth: AuthManager) -> None:
    password = SecretStr("Correct horse battery staple1!")
    user = await auth.register("lu", password, await auth.invite(), name="Lu")
    tokens = await auth.login(user.username, password)
    bearers = KnownBearers(auth)
    for token in (tokens.access_token, tokens.refresh_token):
        assert await bearers.verify_token(token.get_secret_value()) is None
        with pytest.raises(HTTPException):
            await hook_guard(bearers)(f"Bearer {token.get_secret_value()}")


async def test_hook_guard_rejects_invalid_headers(auth: AuthManager) -> None:
    verify = hook_guard(KnownBearers(auth))
    for authorization in (None, "Bearer stranger", "token", "Basic token"):
        with pytest.raises(HTTPException) as denial:
            await verify(authorization)
        assert denial.value.status_code == 401


async def test_hook_sender_demands_a_registered_username() -> None:
    resolve = hook_sender(UserManager(), "claude-native", hook_guard(KnownBearers()))
    with pytest.raises(RuntimeError, match="registry holds no such user"):
        await resolve("lu")
