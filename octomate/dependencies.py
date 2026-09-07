from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from octomate.base import Octomate
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager
from octomate.managers.oauth import OAuthManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import MirrorManager, WorkspaceManager


def application(connection: HTTPConnection) -> Octomate:
    return connection.app


def user_manager(app: Annotated[Octomate, Depends(application)]) -> UserManager:
    return app.users


def thread_manager(app: Annotated[Octomate, Depends(application)]) -> ThreadManager:
    return app.thread_manager


def oauth_manager(app: Annotated[Octomate, Depends(application)]) -> OAuthManager:
    return app.oauth


def workspace_manager(
    app: Annotated[Octomate, Depends(application)],
) -> WorkspaceManager:
    return app.workspaces


def project_manager(app: Annotated[Octomate, Depends(application)]) -> ProjectManager:
    return app.projects


def mirror_manager(app: Annotated[Octomate, Depends(application)]) -> MirrorManager:
    return app.mirrors


def conversation_manager(
    app: Annotated[Octomate, Depends(application)],
) -> ConversationManager:
    return app.conversations


def deferred_action_manager(
    app: Annotated[Octomate, Depends(application)],
) -> DeferredActionManager:
    return app.deferred_actions


def gateway_manager(app: Annotated[Octomate, Depends(application)]) -> GatewayManager:
    return app.gateway
