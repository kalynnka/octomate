"""The hook routers authenticate. Reachability is not a credential: the transport is
plain HTTP on purpose (a native session may run on a different machine than Octomate),
and these routes write a session's prompts and answers into thread history, which agents
read back."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.claude import CLAUDE_HOOK_PATH
from octomate_cli.codex import CODEX_HOOK_PATH
from octomate_cli.deepseek import DEEPSEEK_HOOK_PATH
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, CodexConfig, DeepseekConfig
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.deepseek import DeepseekTentacle

SECRET = SecretStr("the-hook-secret")
EVENT = {"hook_event_name": "SessionEnd", "session_id": "s1"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def client_for(path: str) -> TestClient:
    octomate = Octomate()
    if path == CLAUDE_HOOK_PATH:
        tentacle = ClaudeCodeTentacle(
            "claude", octomate, config=ClaudeCodeConfig(), hook_secret=SECRET
        )
    elif path == CODEX_HOOK_PATH:
        tentacle = CodexTentacle(
            "codex",
            octomate,
            config=CodexConfig(permission_mode="deny_all"),
            hook_secret=SECRET,
        )
    else:
        tentacle = DeepseekTentacle(
            "deepseek", octomate, config=DeepseekConfig(), hook_secret=SECRET
        )
    app = FastAPI()
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
    response = client_for(path).post(
        path,
        json=EVENT,
        headers={"Authorization": f"Bearer {SECRET.get_secret_value()}"},
    )
    assert response.status_code == 200
