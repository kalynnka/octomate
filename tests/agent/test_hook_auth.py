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
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            mcp=octomate.mcp,
        )
    elif path == CODEX_HOOK_PATH:
        tentacle = CodexTentacle(
            "codex",
            octomate,
            config=CodexConfig(permission_mode="auto_review"),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            auth=octomate.auth,
            gateway_manager=octomate.gateway,
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
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    tentacle = ClaudeCodeTentacle(
        "claude",
        host,
        config=ClaudeCodeConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        mcp=host.mcp,
    )
    assert len(tentacle.routers()) == 1


@pytest.mark.parametrize("token", ["wrong", SECRET.get_secret_value()])
async def test_stream_authentication_has_its_own_database_context(token: str) -> None:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    app.connect(
        ClaudeCodeTentacle(
            "claude",
            app,
            config=ClaudeCodeConfig(),
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            mcp=app.mcp,
        )
    )
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


@pytest.mark.parametrize(
    ("tool_name", "stamped"),
    [("mcp__octomate__gateway_teleport", True), ("Bash", False)],
)
def test_a_teleport_call_is_answered_with_its_session_stamped_in(
    tool_name: str, stamped: bool
) -> None:
    """A native call cannot say which session made it; the hook that sees it can,
    and answers the teleport's input back with the session in it. Any other
    tool's call is only observed."""
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": tool_name,
        "tool_input": {"hint": "moving over"},
    }
    with client_for(CLAUDE_HOOK_PATH) as client:
        response = client.post(
            CLAUDE_HOOK_PATH,
            json=event,
            headers={"Authorization": f"Bearer {SECRET.get_secret_value()}"},
        )

    assert response.status_code == 200
    assert response.json() == (
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {"hint": "moving over", "session_id": "s1"},
            }
        }
        if stamped
        else {}
    )
