"""The project ACP endpoint brokers a real scripted stdio peer over WebSocket."""

import json
import os
from pathlib import Path

import acp.transports
import pytest
from anyio import Path as AsyncPath
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import Thread, ThreadMessage
from octomate.tentacles.acp.client import AcpProvider
from tests.agent.test_acp_client import adapter_environment as adapter_environment
from tests.support.users import a_user, auth_config


@pytest.fixture
async def client(
    in_memory_engine: AsyncEngine,
    adapter_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("ACP_TEST_LOG", adapter_environment["ACP_TEST_LOG"])
    monkeypatch.setattr(
        acp.transports,
        "DEFAULT_INHERITED_ENV_VARS",
        [*acp.transports.DEFAULT_INHERITED_ENV_VARS, "ACP_TEST_LOG"],
    )
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    user = await a_user("alice")
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])
    return TestClient(
        app, headers={"Authorization": f"Bearer {key.token.get_secret_value()}"}
    )


def open_session(socket: WebSocketTestSession) -> str:
    socket.send_json(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True}, "terminal": True},
                "_meta": {"client": "retained"},
            },
        }
    )
    initialized = socket.receive_json()
    assert initialized["id"] == "init"
    assert initialized["result"]["protocolVersion"] == 1
    assert initialized["result"]["agentCapabilities"]["loadSession"] is False
    assert initialized["result"]["agentCapabilities"]["sessionCapabilities"] == {}
    assert (
        initialized["result"]["agentCapabilities"]["promptCapabilities"]["image"]
        is True
    )
    assert initialized["result"]["authMethods"] == []
    assert initialized["result"]["_meta"] == {"peer": "retained"}
    socket.send_json(
        {
            "jsonrpc": "2.0",
            "id": "new",
            "method": "session/new",
            "params": {"cwd": "/project", "mcpServers": []},
        }
    )
    response = socket.receive_json()
    assert response["id"] == "new"
    assert response["result"]["_meta"] == {"providerSession": True}
    assert "modes" not in response["result"]
    assert "configOptions" not in response["result"]
    return response["result"]["sessionId"]


@pytest.mark.parametrize("provider", list(AcpProvider))
@pytest.mark.parametrize("cancel", [False, True])
async def test_prompt_updates_permissions_cancellation_and_native_registration(
    client: TestClient,
    provider: AcpProvider,
    cancel: bool,
    adapter_environment: dict[str, str],
) -> None:
    with client.websocket_connect(f"/api/acp/{provider}") as socket:
        session_id = open_session(socket)
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "hello"}],
                    "_meta": {"session_id": "extension, not the target"},
                },
            }
        )
        update, permission = socket.receive_json(), socket.receive_json()
        assert update["method"] == "session/update"
        assert len(update["params"]["update"]["content"]["text"]) == 100_000
        assert permission["method"] == "session/request_permission"
        assert permission["params"]["sessionId"] == session_id
        if cancel:
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
        else:
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": permission["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "yes"}},
                }
            )
        assert socket.receive_json() == {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {"stopReason": "cancelled" if cancel else "end_turn"},
        }
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    records = [json.loads(line) for line in log.splitlines()]
    assert records[0]["message"]["params"]["_meta"] == {"client": "retained"}
    assert records[2]["message"]["params"]["_meta"] == {
        "session_id": "extension, not the target"
    }
    with pytest.raises(ProcessLookupError):
        os.kill(records[0]["pid"], 0)
    async with async_session() as session:
        [thread] = await session.list(Thread)
        [conversation] = await session.list(Conversation)
        assert thread.channel_tentacle_id == provider.native_id
        assert thread.chat_id == session_id
        assert conversation.thread_id == thread.id
        assert conversation.external_id == session_id
        assert not await session.list(ThreadMessage)


def test_unauthorized_connection_starts_no_process(
    client: TestClient, adapter_environment: dict[str, str]
) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        client.websocket_connect(
            "/api/acp/claude", headers={"Authorization": "Bearer wrong"}
        ),
    ):
        pytest.fail("Invalid credentials reached the endpoint")
    assert denial.value.status_code == 401
    assert not Path(adapter_environment["ACP_TEST_LOG"]).exists()


@pytest.mark.parametrize("frame", ["not json", "[]", "{}", "null"])
def test_invalid_frames_close_without_starting_a_process(
    client: TestClient, adapter_environment: dict[str, str], frame: str
) -> None:
    with client.websocket_connect("/api/acp/claude") as socket:
        socket.send_text(frame)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008
    assert not Path(adapter_environment["ACP_TEST_LOG"]).exists()


def test_initialize_is_required_and_cannot_be_repeated(client: TestClient) -> None:
    with client.websocket_connect("/api/acp/codex") as socket:
        socket.send_json(
            {"jsonrpc": "2.0", "id": "early", "method": "session/new", "params": {}}
        )
        assert socket.receive_json()["error"]["code"] == -32600
        open_session(socket)
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "repeat",
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        assert socket.receive_json()["error"]["code"] == -32600


@pytest.mark.parametrize(
    "method", ["session/load", "session/list", "authenticate", "_fail"]
)
def test_unsupported_methods_never_reach_the_provider(
    client: TestClient, adapter_environment: dict[str, str], method: str
) -> None:
    with client.websocket_connect("/api/acp/claude") as socket:
        open_session(socket)
        socket.send_json(
            {"jsonrpc": "2.0", "id": "refused", "method": method, "params": {}}
        )
        assert socket.receive_json()["error"]["code"] == -32601
    assert len(Path(adapter_environment["ACP_TEST_LOG"]).read_text().splitlines()) == 2


def test_unknown_session_and_invalid_prompt_are_rejected(client: TestClient) -> None:
    with client.websocket_connect("/api/acp/claude") as socket:
        session_id = open_session(socket)
        for params in (
            {"sessionId": "someone-else", "prompt": []},
            {"sessionId": session_id, "prompt": "invalid"},
        ):
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "invalid",
                    "method": "session/prompt",
                    "params": params,
                }
            )
            assert socket.receive_json()["error"]["code"] == -32602


@pytest.mark.parametrize("callback_method", ["fs/read_text_file", "terminal/create"])
@pytest.mark.parametrize("error", [False, True])
def test_client_requests_and_errors_are_relayed(
    client: TestClient,
    adapter_environment: dict[str, str],
    callback_method: str,
    error: bool,
) -> None:
    with client.websocket_connect("/api/acp/claude") as socket:
        session_id = open_session(socket)
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "callback"}],
                    "_meta": {"callbackMethod": callback_method},
                },
            }
        )
        socket.receive_json()
        callback = socket.receive_json()
        assert callback["method"] == callback_method
        reply = (
            {
                "error": {
                    "code": -32602,
                    "message": "client refusal",
                    "data": {"reason": "test"},
                }
            }
            if error
            else {
                "result": {
                    **(
                        {"content": "client file"}
                        if callback_method == "fs/read_text_file"
                        else {"terminalId": "client-terminal"}
                    ),
                    "_meta": {"client": True},
                }
            }
        )
        socket.send_json({"jsonrpc": "2.0", "id": callback["id"], **reply})
        assert socket.receive_json()["result"]["stopReason"] == "end_turn"
    records = [
        json.loads(line)
        for line in Path(adapter_environment["ACP_TEST_LOG"]).read_text().splitlines()
    ]
    assert records[-1]["message"] == {"jsonrpc": "2.0", "id": "permission", **reply}
    assert records[0]["message"]["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": True},
        "terminal": True,
    }


def test_provider_errors_reach_the_caller(client: TestClient) -> None:
    with client.websocket_connect("/api/acp/codex") as socket:
        session_id = open_session(socket)
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [],
                    "_meta": {"fail": True},
                },
            }
        )
        assert socket.receive_json() == {
            "jsonrpc": "2.0",
            "id": "prompt",
            "error": {
                "code": -32602,
                "message": "scripted refusal",
                "data": {"retry": False},
            },
        }


def test_disconnect_while_permission_is_pending_reaps_adapter(
    client: TestClient, adapter_environment: dict[str, str]
) -> None:
    with client.websocket_connect("/api/acp/claude") as socket:
        session_id = open_session(socket)
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "wait"}],
                },
            }
        )
        socket.receive_json()
        assert socket.receive_json()["method"] == "session/request_permission"
    record = json.loads(
        Path(adapter_environment["ACP_TEST_LOG"]).read_text().splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)
