"""The hook routers authenticate. Reachability is not a credential: the transport is
plain HTTP on purpose (a native session may run on a different machine than Octomate),
and these routes write a session's prompts and answers into thread history, which agents
read back."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.tentacles.claude import CLAUDE_HOOK_PATH
from octomate_cli.tentacles.codex import CODEX_HOOK_PATH
from octomate_cli.tentacles.deepseek import DEEPSEEK_HOOK_PATH
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, CodexConfig, DeepseekConfig
from octomate.managers.user import UserManager
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from tests.support.agents import CLAUDE_MODELS, CODEX_MODELS, DEEPSEEK_MODELS
from tests.support.config import registered

SECRET = SecretStr("the-hook-secret")
EVENT = {"hook_event_name": "SessionEnd", "session_id": "s1"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def client_for(path: str) -> TestClient:
    config = registered(SECRET.get_secret_value())
    octomate = Octomate(config=config, users=UserManager(config.users))
    if path == CLAUDE_HOOK_PATH:
        tentacle = ClaudeCodeTentacle(
            "claude",
            octomate,
            config=ClaudeCodeConfig(models=set(CLAUDE_MODELS)),
        )
    elif path == CODEX_HOOK_PATH:
        tentacle = CodexTentacle(
            "codex",
            octomate,
            config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
        )
    else:
        tentacle = DeepseekTentacle(
            "deepseek",
            octomate,
            config=DeepseekConfig(models=set(DEEPSEEK_MODELS)),
        )

    # Entering the client runs the lifespan: the registered user gets their
    # registry row, the way the real app reconciles before serving — the hook
    # handlers resolve the verified bearer's own profile against it.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await octomate.users.reconcile()
        yield

    app = FastAPI(lifespan=lifespan)
    for router in tentacle.routers():
        app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path", [CLAUDE_HOOK_PATH, CODEX_HOOK_PATH, DEEPSEEK_HOOK_PATH]
)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-credential"),
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong-secret"),
        pytest.param({"Authorization": "the-hook-secret"}, id="bare-secret-no-scheme"),
    ],
)
def test_an_unauthenticated_hook_is_refused(path: str, headers: dict[str, str]) -> None:
    response = client_for(path).post(path, json=EVENT, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path", [CLAUDE_HOOK_PATH, CODEX_HOOK_PATH, DEEPSEEK_HOOK_PATH]
)
def test_the_configured_secret_is_accepted(path: str) -> None:
    with client_for(path) as client:
        response = client.post(
            path,
            json=EVENT,
            headers={"Authorization": f"Bearer {SECRET.get_secret_value()}"},
        )
    assert response.status_code == 200


def test_a_hook_router_refuses_to_mount_for_nobody() -> None:
    # A deployment where no user carries a secret would serve a router no
    # human's machine could reach — the boot says so instead.
    tentacle = ClaudeCodeTentacle(
        "claude",
        Octomate(),
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS)),
    )
    with pytest.raises(RuntimeError, match="no registered user carries a secret"):
        tentacle.routers()
