"""The hook routers authenticate. Reachability is not a credential: the transport is
plain HTTP on purpose (a native session may run on a different machine than Octomate),
and these routes write a session's prompts and answers into thread history, which agents
read back."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import Context

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.tentacles.claude import CLAUDE_HOOK_PATH
from octomate_cli.tentacles.codex import CODEX_HOOK_PATH
from octomate_cli.tentacles.deepseek import DEEPSEEK_HOOK_PATH
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse

from octomate import Octomate
from octomate.config import (
    ClaudeCodeConfig,
    CodexConfig,
    DeepseekConfig,
    OctomateConfig,
)
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from tests.support.users import a_api_key, a_user, auth_config

SECRET = SecretStr("the-hook-secret")
EVENT = {"hook_event_name": "SessionEnd", "session_id": "s1"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def client_for(path: str) -> TestClient:
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    if path == CLAUDE_HOOK_PATH:
        tentacle = ClaudeCodeTentacle(
            "claude",
            octomate,
            config=ClaudeCodeConfig(),
        )
    elif path == CODEX_HOOK_PATH:
        tentacle = CodexTentacle(
            "codex",
            octomate,
            config=CodexConfig(permission_mode="deny_all"),
        )
    else:
        tentacle = DeepseekTentacle(
            "deepseek",
            octomate,
            config=DeepseekConfig(),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        user = await a_user()
        await a_api_key(user, SECRET.get_secret_value())
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
def test_the_api_token_is_accepted(path: str) -> None:
    with client_for(path) as client:
        response = client.post(
            path,
            json=EVENT,
            headers={"Authorization": f"Bearer {SECRET.get_secret_value()}"},
        )
    assert response.status_code == 200


def test_a_hook_router_mounts_before_any_user_registers() -> None:
    tentacle = ClaudeCodeTentacle(
        "claude",
        Octomate(config=OctomateConfig(auth=auth_config())),
        config=ClaudeCodeConfig(),
    )
    assert len(tentacle.routers()) == 1


@pytest.mark.parametrize("token", ["wrong", SECRET.get_secret_value()])
async def test_stream_authentication_has_its_own_database_context(token: str) -> None:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    app.connect(ClaudeCodeTentacle("claude", app, config=ClaudeCodeConfig()))
    user = await a_user()
    await a_api_key(user, SECRET.get_secret_value())

    def connect() -> None:
        with TestClient(app).websocket_connect(
            f"{CLAUDE_HOOK_PATH}/stream",
            headers={"Authorization": f"Bearer {token}"},
        ):
            pass

    # Uvicorn's request tasks do not inherit the fixture's active materia.
    if token == SECRET.get_secret_value():
        Context().run(connect)
    else:
        with pytest.raises(WebSocketDenialResponse) as denial:
            Context().run(connect)
        assert denial.value.status_code == 401
