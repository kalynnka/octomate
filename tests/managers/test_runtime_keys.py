"""Runtime API keys are shared database state, including their idle deadline."""

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import ClassVar, Literal

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

import octomate.managers.auth as auth_module
from octomate.database import async_session
from octomate.managers.auth import AuthManager, InvalidCredentials
from octomate.schemas.auth import ApiKey, RuntimeApiKey, UserApiKey
from octomate.schemas.user import User
from tests.support.users import a_user, auth_config, runtime_api_keys


class Clock(datetime):
    current: ClassVar[datetime] = datetime(2030, 1, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls.current


@pytest.fixture(autouse=True)
async def runtime_db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
async def auth(monkeypatch: pytest.MonkeyPatch) -> AuthManager:
    Clock.current = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(auth_module, "datetime", Clock)
    config = auth_config()
    config.runtime_api_key_lifetime = timedelta(hours=1)
    return AuthManager(config)


async def test_three_cores_share_creation_renewal_and_expiry_rotation(
    auth: AuthManager,
) -> None:
    user = await a_user()
    cores = [auth, AuthManager(auth.config), AuthManager(auth.config)]
    issued = await asyncio.gather(
        *(
            core.runtime_api_key(user.id, agent_id="codex", scopes=["hooks", "mcp"])
            for core in cores
        )
    )
    assert len({key.id for key in issued}) == 1
    assert len({key.bearer for key in issued}) == 1
    original = issued[0]
    assert original.bearer is not None
    Clock.current += timedelta(minutes=50)
    authenticated = await asyncio.gather(
        *(core.authenticate_api_key(original.bearer, scope="mcp") for core in cores)
    )
    assert all(key is not None for key in authenticated)
    Clock.current += timedelta(minutes=50)
    restarted = AuthManager(auth.config)
    reused = await restarted.runtime_api_key(
        user.id, agent_id="codex", scopes=["mcp", "hooks"]
    )
    assert reused.bearer is not None
    assert reused.bearer == original.bearer
    assert reused.expires_at == Clock.current + timedelta(hours=1)
    Clock.current += timedelta(hours=2)
    assert await auth.authenticate_api_key(original.bearer, scope="mcp") is None
    replacements = await asyncio.gather(
        *(
            core.runtime_api_key(user.id, agent_id="codex", scopes=["hooks", "mcp"])
            for core in cores
        )
    )
    assert len({key.bearer for key in replacements}) == 1
    assert replacements[0].bearer != original.bearer
    assert replacements[0].id == original.id
    assert await auth.authenticate_api_key(original.bearer, scope="mcp") is None
    assert len(await runtime_api_keys(user.id)) == 1


async def test_acquisition_alone_slides_idle_expiry(auth: AuthManager) -> None:
    user = await a_user()
    original = await auth.runtime_api_key(user.id, agent_id="acp", scopes=["mcp"])
    assert original.bearer is not None
    for _ in range(4):
        Clock.current += timedelta(minutes=40)
        reused = await AuthManager(auth.config).runtime_api_key(
            user.id, agent_id="acp", scopes=["mcp"]
        )
        assert reused.bearer is not None
        assert reused.bearer == original.bearer
        assert reused.expires_at == Clock.current + timedelta(hours=1)
    assert len(await runtime_api_keys(user.id)) == 1


async def test_wrong_scope_and_invalid_bearer_do_not_renew(auth: AuthManager) -> None:
    user = await a_user()
    issued = await auth.runtime_api_key(user.id, agent_id="acp", scopes=["mcp"])
    assert issued.bearer is not None
    Clock.current += timedelta(minutes=40)
    assert await auth.authenticate_api_key(issued.bearer, scope="hooks") is None
    assert await auth.authenticate_api_key(SecretStr("invalid"), scope="mcp") is None
    [stored] = await runtime_api_keys(user.id)
    assert stored.expires_at == issued.expires_at
    Clock.current += timedelta(minutes=20)
    assert await auth.authenticate_api_key(issued.bearer, scope="mcp") is None


async def test_personal_keys_keep_fixed_expiry(auth: AuthManager) -> None:
    user = await a_user()
    issued = await auth.create_api_key(
        user.id,
        name="personal",
        scopes=["mcp"],
        expires_at=Clock.current + timedelta(hours=1),
    )
    assert issued.bearer is not None
    Clock.current += timedelta(minutes=50)
    authenticated = await auth.authenticate_api_key(issued.bearer, scope="mcp")
    assert authenticated is not None
    assert authenticated.expires_at == issued.expires_at
    Clock.current += timedelta(minutes=10)
    assert await auth.authenticate_api_key(issued.bearer, scope="mcp") is None


async def test_revocation_wins_over_concurrent_use_and_survives_restart(
    auth: AuthManager,
) -> None:
    user = await a_user()
    issued = await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert issued.bearer is not None
    other = AuthManager(auth.config)
    Clock.current += timedelta(minutes=40)
    await asyncio.gather(
        auth.revoke_api_key(user.id, issued.id),
        other.authenticate_api_key(issued.bearer, scope="mcp"),
    )
    restarted = AuthManager(auth.config)
    assert await restarted.authenticate_api_key(issued.bearer, scope="mcp") is None
    with pytest.raises(InvalidCredentials):
        await restarted.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    [stored] = await runtime_api_keys(user.id)
    assert stored.revoked_at is not None


async def test_users_and_agents_have_distinct_keys(auth: AuthManager) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    issued = await asyncio.gather(
        auth.runtime_api_key(alice.id, agent_id="codex", scopes=["hooks", "mcp"]),
        auth.runtime_api_key(alice.id, agent_id="acp", scopes=["mcp"]),
        auth.runtime_api_key(bob.id, agent_id="codex", scopes=["hooks", "mcp"]),
    )
    assert len({key.bearer for key in issued}) == 3
    assert issued[1].bearer is not None
    assert await auth.authenticate_api_key(issued[1].bearer, scope="hooks") is None
    async with async_session() as session:
        assert len(await session.list(RuntimeApiKey, limit=None)) == 3


async def test_mismatched_replica_secret_cannot_replace_a_live_key(
    auth: AuthManager,
) -> None:
    user = await a_user()
    issued = await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert issued.bearer is not None
    other_config = auth.config.model_copy(
        update={"api_key_salt": SecretStr("different-deployment-secret")}
    )
    other = AuthManager(other_config)
    with pytest.raises(ValueError, match="auth secrets"):
        await other.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert await auth.authenticate_api_key(issued.bearer, scope="mcp") is not None
    [stored] = await runtime_api_keys(user.id)
    assert stored.key_hash == issued.key_hash


async def test_concurrent_usage_with_independent_core_managers(
    auth: AuthManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_module, "datetime", datetime)
    user = await a_user()
    cores = [auth, AuthManager(auth.config), AuthManager(auth.config)]
    issued = await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert issued.bearer is not None
    results = await asyncio.gather(
        *(
            cores[index % 3].authenticate_api_key(issued.bearer, scope="mcp")
            for index in range(30)
        )
    )
    assert all(key is not None for key in results)
    [stored] = await runtime_api_keys(user.id)
    assert stored.expires_at is not None
    assert issued.expires_at is not None
    assert stored.expires_at >= issued.expires_at


async def test_base_queries_load_concrete_keys_and_personal_queries_exclude_runtimes(
    auth: AuthManager,
) -> None:
    user = await a_user()
    personal = await auth.create_api_key(user.id, name="Laptop", scopes=["mcp"])
    assert personal.bearer is not None
    runtime = await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert runtime.bearer is not None
    assert personal.model_dump()["kind"] == "user"
    assert runtime.model_dump()["kind"] == "runtime"
    assert "runtime_nonce" not in runtime.model_dump()
    assert "bearer" not in runtime.model_dump()
    assert runtime.bearer.get_secret_value() not in runtime.model_dump_json()
    assert runtime.bearer.get_secret_value() not in repr(runtime)
    assert runtime.bearer.get_secret_value() not in str(runtime)
    assert runtime.bearer.get_secret_value() not in f"{runtime}"
    async with async_session() as session:
        loaded = await session.get(ApiKey, runtime.id)
        assert isinstance(loaded, RuntimeApiKey)
        assert loaded.bearer is None
        assert loaded.runtime_nonce == runtime.runtime_nonce
        assert await session.get(UserApiKey, runtime.id) is None
        assert {type(key) for key in await session.list(ApiKey)} == {
            UserApiKey,
            RuntimeApiKey,
        }
        [listed] = await session.list(UserApiKey)
        assert listed.id == personal.id
    assert [key.id for key in await auth.list_api_keys(user.id)] == [personal.id]
    await auth.revoke_api_key(user.id, runtime.id)
    assert await auth.authenticate_api_key(runtime.bearer, scope="mcp") is None


async def test_user_deletion_removes_base_and_subtype_key_rows(
    auth: AuthManager,
) -> None:
    user = await a_user()
    await auth.create_api_key(user.id, name="Laptop", scopes=["mcp"])
    await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()
    async with async_session() as session:
        assert not await session.list(ApiKey)
        assert not await session.list(UserApiKey)
        assert not await session.list(RuntimeApiKey)


@pytest.mark.parametrize("change", ["revoke", "rotate", "expire"])
async def test_authentication_rechecks_key_after_waiting_for_lock(
    auth: AuthManager, change: Literal["revoke", "rotate", "expire"]
) -> None:
    user = await a_user()
    issued = await auth.runtime_api_key(user.id, agent_id="codex", scopes=["mcp"])
    assert issued.bearer is not None
    waiting = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                waiting.set()
            return await super().acquire()

    lock = ObservedLock()
    auth.locks[f"api-key:{user.id}"] = lock
    async with asyncio.TaskGroup() as tasks:
        async with lock:
            request = tasks.create_task(
                auth.authenticate_api_key(issued.bearer, scope="mcp")
            )
            await asyncio.wait_for(waiting.wait(), timeout=5)
            other = AuthManager(auth.config)
            if change == "revoke":
                await other.revoke_api_key(user.id, issued.id)
            elif change == "rotate":
                await other.runtime_api_key(
                    user.id, agent_id="codex", scopes=["hooks", "mcp"]
                )
            else:
                Clock.current += timedelta(hours=1)
    assert request.result() is None
