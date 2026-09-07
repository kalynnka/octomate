import asyncio
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.users import UsersConfig
from octomate.managers.oauth import OAuthManager
from octomate.managers.user import UserManager
from octomate.oauth.routes import oauth_manager


def test_manager_construction_is_independent() -> None:
    config = UsersConfig()
    users = UserManager(config)
    other = UserManager()

    assert other is not users
    assert users.config is config
    assert other.lock(("slack", "U1")) is not users.lock(("slack", "U1"))


def test_dependency_reuses_the_configured_manager_across_requests() -> None:
    config = UsersConfig()
    octomate = Octomate(users=UserManager(config))
    key = (uuid7(), "linear")
    lock = octomate.oauth.lock(key)
    app = FastAPI()
    app.state.octomate = octomate

    @app.get("/manager")
    def get_manager(
        manager: Annotated[OAuthManager, Depends(oauth_manager)],
    ) -> int:
        assert manager.users is octomate.users
        assert manager.users.config is config
        assert manager.lock(key) is lock
        return id(manager)

    with TestClient(app) as client:
        assert client.get("/manager").json() == id(octomate.oauth)
        assert client.get("/manager").json() == id(octomate.oauth)
        operation = client.get("/openapi.json").json()["paths"]["/manager"]["get"]
        assert "parameters" not in operation

    assert oauth_manager.cache_info().misses == 1
    assert oauth_manager.cache_info().hits == 1


def test_separate_apps_keep_their_own_configured_managers() -> None:
    first, second = Octomate(), Octomate()
    first_app, second_app = FastAPI(), FastAPI()
    first_app.state.octomate = first
    second_app.state.octomate = second

    assert oauth_manager(first_app) is first.oauth
    assert oauth_manager(second_app) is second.oauth
    assert oauth_manager(first_app) is not oauth_manager(second_app)


async def test_cached_dependency_preserves_the_lock_in_use() -> None:
    octomate = Octomate()
    app = FastAPI()
    app.state.octomate = octomate
    key = (uuid7(), "linear")
    acquired = asyncio.Event()

    async def contender() -> None:
        async with oauth_manager(app).lock(key):
            acquired.set()

    async with octomate.oauth.lock(key):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0)
        assert not acquired.is_set()

    await asyncio.wait_for(task, timeout=1)
    assert acquired.is_set()
