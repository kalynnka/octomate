from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.auth import AuthConfig
from octomate.database import AsyncSession, async_session
from octomate.managers.auth import AuthManager, InvalidCredentials
from octomate.schemas.auth import SessionTokens, UserApiKey, UserSession
from octomate.schemas.user import User

PASSWORD = SecretStr("a correct horse battery staple")


@pytest.fixture(autouse=True)
async def auth_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def auth() -> AuthManager:
    return AuthManager(
        AuthConfig(
            access_token_salt=SecretStr("access-token-test-salt"),
            refresh_token_salt=SecretStr("refresh-token-test-salt"),
            api_key_salt=SecretStr("api-key-test-salt"),
        )
    )


@pytest.fixture
async def user(auth: AuthManager) -> User:
    user = User(
        username="alice",
        password_hash=await auth.hash_password(PASSWORD),
    )
    async with async_session() as session:
        session.add(user)
        await session.commit()
    return user


async def test_login_persists_hashes_and_redacts_credentials(
    auth: AuthManager, user: User
) -> None:
    tokens = await auth.login(user.username, PASSWORD)

    async with async_session() as session:
        stored_user = await session.get(User, user.id)
        stored_session = await session.get(UserSession, tokens.session_id)
    assert stored_user is not None
    assert stored_user.password_hash is not None
    assert stored_user.password_hash.get_secret_value().startswith("$argon2id$")
    assert PasswordHasher().verify(
        stored_user.password_hash.get_secret_value(), PASSWORD.get_secret_value()
    )
    assert "password_hash" not in stored_user.model_dump()
    assert stored_session is not None
    assert stored_session.access_token_hash != tokens.access_token
    assert stored_session.refresh_token_hash != tokens.refresh_token
    assert len(stored_session.access_token_hash.get_secret_value()) == 64
    assert "access_token_hash" not in stored_session.model_dump()
    assert "refresh_token_hash" not in stored_session.model_dump()
    assert tokens.access_token != tokens.refresh_token
    assert tokens.access_expires_at < tokens.refresh_expires_at
    for secret in (PASSWORD, tokens.access_token, tokens.refresh_token):
        assert secret.get_secret_value() not in repr(tokens)
        assert secret.get_secret_value() not in tokens.model_dump_json()
        assert secret.get_secret_value() not in stored_session.model_dump_json()
    principal = await AuthManager(auth.config).authenticate_session(tokens.access_token)
    assert principal is not None
    assert principal.user_id == user.id


@pytest.mark.parametrize("username", ["alice", "unknown", "unenrolled"])
async def test_login_failures_are_generic_and_issue_nothing(
    auth: AuthManager, user: User, username: str
) -> None:
    async with async_session() as session:
        session.add(User(username="unenrolled"))
        await session.commit()
    with pytest.raises(InvalidCredentials, match=r"^Invalid or expired credentials$"):
        await auth.login(username, SecretStr("wrong password"))
    async with async_session() as session:
        assert list(await session.list(UserSession, limit=None)) == []


async def test_login_rehashes_an_outdated_password(
    auth: AuthManager, user: User
) -> None:
    old_hash = SecretStr(PasswordHasher(time_cost=1).hash(PASSWORD.get_secret_value()))
    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.password_hash = old_hash
        await session.commit()

    await auth.login(user.username, PASSWORD)

    async with async_session() as session:
        stored = await session.get(User, user.id)
    assert stored is not None
    assert stored.password_hash is not None
    assert stored.password_hash != old_hash
    assert not PasswordHasher().check_needs_rehash(
        stored.password_hash.get_secret_value()
    )


async def test_refresh_rotates_both_tokens_without_extending_the_session(
    auth: AuthManager, user: User
) -> None:
    first = await auth.login(user.username, PASSWORD)
    with patch("octomate.managers.auth.datetime") as clock:
        clock.now.return_value = first.refresh_expires_at - timedelta(minutes=1)
        assert await auth.authenticate_session(first.access_token) is None
        second = await auth.refresh_session(first.refresh_token)

    assert first.session_id == second.session_id
    assert first.user_id == second.user_id
    assert first.access_token != second.access_token
    assert first.refresh_token != second.refresh_token
    assert first.refresh_expires_at == second.refresh_expires_at
    assert second.access_expires_at == second.refresh_expires_at
    assert await auth.authenticate_session(first.access_token) is None
    with pytest.raises(InvalidCredentials):
        await auth.refresh_session(first.refresh_token)
    assert await auth.authenticate_session(second.access_token) is not None


async def test_simultaneous_refreshes_only_issue_one_pair(
    auth: AuthManager, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await auth.login(user.username, PASSWORD)
    other = AuthManager(auth.config)
    barrier = asyncio.Barrier(2)
    commit = AsyncSession.commit

    async def simultaneous_commit(session: AsyncSession) -> None:
        await barrier.wait()
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", simultaneous_commit)
    results = await asyncio.wait_for(
        asyncio.gather(
            auth.refresh_session(first.refresh_token),
            other.refresh_session(first.refresh_token),
            return_exceptions=True,
        ),
        timeout=5,
    )
    issued = [result for result in results if isinstance(result, SessionTokens)]
    assert len(issued) == 1
    assert sum(isinstance(result, InvalidCredentials) for result in results) == 1
    assert await auth.authenticate_session(issued[0].access_token) is not None


async def test_access_and_refresh_expire_at_the_deadline(
    auth: AuthManager, user: User
) -> None:
    tokens = await auth.login(user.username, PASSWORD)
    with patch("octomate.managers.auth.datetime") as clock:
        clock.now.return_value = tokens.access_expires_at
        assert await auth.authenticate_session(tokens.access_token) is None
        clock.now.return_value = tokens.refresh_expires_at
        with pytest.raises(InvalidCredentials):
            await auth.refresh_session(tokens.refresh_token)


async def test_only_owner_can_revoke_a_session_and_logout_revokes_both_tokens(
    auth: AuthManager, user: User
) -> None:
    tokens = await auth.login(user.username, PASSWORD)
    with pytest.raises(InvalidCredentials):
        await auth.revoke_session(uuid7(), tokens.session_id)
    assert await auth.authenticate_session(tokens.access_token) is not None
    await auth.revoke_session(user.id, tokens.session_id)
    await auth.revoke_session(user.id, tokens.session_id)
    assert await auth.authenticate_session(tokens.access_token) is None
    with pytest.raises(InvalidCredentials):
        await auth.refresh_session(tokens.refresh_token)


async def test_api_keys_are_independent_named_scoped_credentials(
    auth: AuthManager, user: User
) -> None:
    hook = await auth.create_api_key(user.id, name="laptop", scopes=["hooks"])
    mcp = await auth.create_api_key(user.id, name="editor", scopes=["mcp"])
    assert hook.token != mcp.token
    async with async_session() as session:
        stored = await session.get(UserApiKey, hook.key.id)
    assert stored is not None
    assert stored.name == "laptop"
    assert stored.key_hash != hook.token
    assert len(stored.key_hash.get_secret_value()) == 64
    assert "key_hash" not in stored.model_dump()
    assert hook.token.get_secret_value() not in stored.model_dump_json()
    assert hook.token.get_secret_value() not in hook.model_dump_json()
    assert hook.token.get_secret_value() not in repr(hook)
    assert stored.key_prefix == hook.token.get_secret_value()[:12]

    restarted = AuthManager(auth.config)
    verified = await restarted.authenticate_api_key(hook.token, scope="hooks")
    assert verified is not None
    assert verified.user_id == user.id
    assert await restarted.authenticate_api_key(hook.token, scope="mcp") is None
    with pytest.raises(InvalidCredentials):
        await auth.revoke_api_key(uuid7(), hook.key.id)
    await auth.revoke_api_key(user.id, hook.key.id)
    await auth.revoke_api_key(user.id, hook.key.id)
    assert await restarted.authenticate_api_key(hook.token, scope="hooks") is None
    assert await restarted.authenticate_api_key(mcp.token, scope="mcp") is not None


async def test_credential_types_cannot_be_substituted(
    auth: AuthManager, user: User
) -> None:
    tokens = await auth.login(user.username, PASSWORD)
    key = await auth.create_api_key(user.id, name="editor", scopes=["hooks", "mcp"])
    for token in (tokens.access_token, tokens.refresh_token, SecretStr("unknown")):
        assert await auth.authenticate_api_key(token, scope="mcp") is None
    for token in (key.token, tokens.refresh_token, SecretStr("unknown")):
        assert await auth.authenticate_session(token) is None
    for token in (key.token, tokens.access_token, SecretStr("unknown")):
        with pytest.raises(InvalidCredentials):
            await auth.refresh_session(token)


async def test_api_key_expiry_is_enforced(auth: AuthManager, user: User) -> None:
    deadline = datetime.now(UTC) + timedelta(days=1)
    key = await auth.create_api_key(
        user.id, name="temporary", scopes=["mcp"], expires_at=deadline
    )
    with patch("octomate.managers.auth.datetime") as clock:
        clock.now.return_value = deadline
        assert await auth.authenticate_api_key(key.token, scope="mcp") is None
    with pytest.raises(ValidationError, match="timezone"):
        await auth.create_api_key(
            user.id,
            name="invalid",
            scopes=["mcp"],
            expires_at=deadline.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="API key expiry"):
        await auth.create_api_key(
            user.id, name="invalid", scopes=["mcp"], expires_at=datetime.now(UTC)
        )


async def test_api_key_requires_an_existing_owner_and_nonempty_scopes(
    auth: AuthManager, user: User
) -> None:
    with pytest.raises(InvalidCredentials):
        await auth.create_api_key(uuid7(), name="missing", scopes=["mcp"])
    with pytest.raises(ValidationError):
        await auth.create_api_key(user.id, name="empty", scopes=[])
    async with async_session() as session:
        assert list(await session.list(UserApiKey, limit=None)) == []


def test_config_requires_secrets_without_printing_their_values() -> None:
    with pytest.raises(ValidationError) as caught:
        AuthConfig(
            access_token_salt=SecretStr("too-short"),
            refresh_token_salt=SecretStr("refresh-token-test-salt"),
            api_key_salt=SecretStr("api-key-test-salt"),
        )
    assert "too-short" not in str(caught.value)
