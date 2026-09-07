import asyncio
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.users import UsersConfig
from octomate.dependencies import (
    application,
    conversation_manager,
    deferred_action_manager,
    gateway_manager,
    mirror_manager,
    oauth_manager,
    project_manager,
    thread_manager,
    user_manager,
    workspace_manager,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager
from octomate.managers.oauth import OAuthManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import MirrorManager, WorkspaceManager


def test_manager_construction_is_independent() -> None:
    config = UsersConfig()
    users = UserManager(config)
    other = UserManager()

    assert other is not users
    assert users.config is config
    assert other.lock(("slack", "U1")) is not users.lock(("slack", "U1"))


async def test_dependency_reuses_the_configured_manager_across_requests() -> None:
    config = UsersConfig()
    octomate = Octomate(users=UserManager(config))
    key = (uuid7(), "linear")
    lock = octomate.oauth.lock(key)
    assert isinstance(octomate, FastAPI)

    @octomate.get("/manager")
    def get_manager(
        app: Annotated[Octomate, Depends(application)],
        manager: Annotated[OAuthManager, Depends(oauth_manager)],
    ) -> int:
        assert app is octomate
        assert manager.users is octomate.users
        assert manager.users.config is config
        assert manager.lock(key) is lock
        return id(manager)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=octomate), base_url="http://test"
    ) as client:
        assert (await client.get("/manager")).json() == id(octomate.oauth)
        assert (await client.get("/manager")).json() == id(octomate.oauth)
        operation = (await client.get("/openapi.json")).json()["paths"]["/manager"][
            "get"
        ]
        assert "parameters" not in operation


def test_separate_apps_keep_their_own_configured_managers() -> None:
    first, second = Octomate(), Octomate()
    assert oauth_manager(first) is first.oauth
    assert oauth_manager(second) is second.oauth
    assert first.oauth is not second.oauth
    assert first.thread_manager is not second.thread_manager


async def test_dependency_preserves_the_lock_in_use() -> None:
    octomate = Octomate()
    key = (uuid7(), "linear")
    acquired = asyncio.Event()

    async def contender() -> None:
        async with oauth_manager(octomate).lock(key):
            acquired.set()

    async with octomate.oauth.lock(key):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0)
        assert not acquired.is_set()

    await asyncio.wait_for(task, timeout=1)
    assert acquired.is_set()


async def test_dependencies_share_the_hosts_manager_graph() -> None:
    octomate = Octomate()

    @octomate.get("/managers")
    def managers(
        users: Annotated[UserManager, Depends(user_manager)],
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        oauth: Annotated[OAuthManager, Depends(oauth_manager)],
        workspaces: Annotated[WorkspaceManager, Depends(workspace_manager)],
        projects: Annotated[ProjectManager, Depends(project_manager)],
        mirrors: Annotated[MirrorManager, Depends(mirror_manager)],
        conversations: Annotated[ConversationManager, Depends(conversation_manager)],
        actions: Annotated[DeferredActionManager, Depends(deferred_action_manager)],
        gateway: Annotated[GatewayManager, Depends(gateway_manager)],
    ) -> bool:
        assert users is threads.users is oauth.users is octomate.users
        assert threads is octomate.thread_manager
        assert oauth is octomate.oauth
        assert workspaces is octomate.workspaces
        assert projects is workspaces.projects is octomate.projects
        assert mirrors is workspaces.mirrors is octomate.mirrors
        assert conversations is octomate.conversations
        assert actions is octomate.deferred_actions
        assert gateway is octomate.gateway
        return True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=octomate), base_url="http://test"
    ) as client:
        assert (await client.get("/managers")).json() is True


async def test_a_dependency_override_leaves_the_app_managers_intact() -> None:
    octomate = Octomate()
    users = UserManager()
    octomate.dependency_overrides[user_manager] = lambda: users

    @octomate.get("/managers")
    def managers(
        injected_users: Annotated[UserManager, Depends(user_manager)],
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        oauth: Annotated[OAuthManager, Depends(oauth_manager)],
    ) -> list[int]:
        assert injected_users is users
        assert threads is octomate.thread_manager
        assert oauth is octomate.oauth
        assert threads.users is oauth.users is octomate.users
        return [id(threads), id(oauth)]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=octomate), base_url="http://test"
    ) as client:
        first = (await client.get("/managers")).json()
        assert (await client.get("/managers")).json() == first

    assert first == [id(octomate.thread_manager), id(octomate.oauth)]
    assert octomate.thread_manager.users is octomate.oauth.users is octomate.users
