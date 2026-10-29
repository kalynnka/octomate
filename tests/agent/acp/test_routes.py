"""The public ACP endpoint drives registered agents through the real reflex graph."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ToolPermissionContext
from claude_agent_sdk.types import Message as ClaudeMessage
from fastapi.testclient import TestClient
from pydantic import JsonValue
from pydantic_ai.capabilities import Toolset
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.harness.events import (
    ReflexAsleepEvent,
    RunEndedEvent,
    RunStartedEvent,
)
from octomate.config import OctomateConfig
from octomate.config.agents import ClaudeCodeConfig
from octomate.database import async_session
from octomate.nerve import ThreadTopic
from octomate.schemas.conversation import Conversation
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import Thread, ThreadMessage
from octomate.tentacles.acp.channel import AcpChannelTentacle
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude import ink as claude_ink
from octomate.tentacles.inkling.base import InklingTentacle
from tests.agent.claude.test_tentacle import FakeClaudeClient
from tests.support.agents import ScriptedStream, ScriptedTurn
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user, auth_config


@pytest.fixture
async def client(in_memory_engine: AsyncEngine) -> TestClient:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    for name in ("first", "second"):
        model = FunctionModel(
            stream_function=ScriptedStream(turns=[f"{name} answer"] * 20)
        )
        app.connect(
            InklingTentacle(
                id=name,
                octomate=app,
                models={
                    "test": model,
                    "alternate": FunctionModel(
                        stream_function=ScriptedStream(
                            turns=[f"{name} alternate answer"] * 20
                        )
                    ),
                },
                gateway=False,
            )
        )
    app.connect(AcpChannelTentacle(id="bridge", octomate=app))
    user = await a_user("alice")
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="ACP client", scopes=["acp"])
    return TestClient(
        app, headers={"Authorization": f"Bearer {key.token.get_secret_value()}"}
    )


def rpc(
    socket: WebSocketTestSession,
    method: str,
    params: dict[str, JsonValue],
    request_id: str = "call",
) -> list[dict[str, JsonValue]]:
    socket.send_json(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    frames = []
    while True:
        frame = socket.receive_json()
        frames.append(frame)
        if frame.get("id") == request_id:
            return frames


def initialize(socket: WebSocketTestSession, *, elicitation: bool = False) -> None:
    frames = rpc(
        socket,
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {"elicitation": {"form": {}}} if elicitation else {},
        },
    )
    result = frames[-1]["result"]
    assert isinstance(result, dict)
    assert result["agentInfo"] == {"name": "octomate", "version": "1"}
    assert result["agentCapabilities"] == {
        "loadSession": True,
        "mcpCapabilities": {"acp": False, "http": False, "sse": False},
        "promptCapabilities": {
            "audio": False,
            "embeddedContext": False,
            "image": False,
        },
        "sessionCapabilities": {"list": {}, "resume": {}},
    }


def new_session(socket: WebSocketTestSession) -> str:
    frames = rpc(socket, "session/new", {"cwd": "/unregistered", "mcpServers": []})
    result = frames[-1]["result"]
    assert isinstance(result, dict)
    assert isinstance(result["sessionId"], str)
    return result["sessionId"]


def prompt(socket: WebSocketTestSession, session_id: str) -> list[dict[str, JsonValue]]:
    return rpc(
        socket,
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello"}]},
    )


@pytest.mark.parametrize(
    ("choice", "answer"),
    [("second:test", "second answer"), ("first:alternate", "first alternate answer")],
)
async def test_unified_model_selection_stream_and_history(
    client: TestClient, choice: str, answer: str
) -> None:
    agent_id, model = choice.split(":")
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
        app = client.app
        assert isinstance(app, Octomate)
        thread = await app.threads.get(UUID(session_id), with_messages=False)
        assert thread is not None
        with app.nerve.subscribe(ThreadTopic(thread.key)) as events:
            first = prompt(socket, session_id)
            heard = []
            while True:
                event = (await events.receive()).event
                heard.append(event)
                if isinstance(event, ReflexAsleepEvent):
                    break
        assert sum(isinstance(event, RunStartedEvent) for event in heard) == 1
        assert sum(isinstance(event, RunEndedEvent) for event in heard) == 1
        assert first[-1]["result"] == {"stopReason": "end_turn"}
        assert "first answer" in str(first)
        selected = rpc(
            socket,
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": choice},
        )
        assert "config_option_update" in str(selected)
        assert choice in str(selected)
        second = prompt(socket, session_id)
        assert answer in str(second)
    async with async_session() as session:
        [thread] = await session.list(Thread)
        assert str(thread.id) == session_id
        assert thread.active_agent_tentacle_id == agent_id
        assert thread.active_model == model
        conversations = await session.list(Conversation)
        assert {conversation.agent_tentacle_id for conversation in conversations} == {
            "first",
            agent_id,
        }
        assert all(
            conversation.thread_id == thread.id for conversation in conversations
        )
        assert len(await session.list(AgentRun)) == 2
        assert len(await session.list(ThreadMessage)) == 4


@pytest.mark.parametrize("method", ["session/load", "session/resume"])
@pytest.mark.parametrize(
    ("choice", "answer"),
    [("second:test", "second answer"), ("first:alternate", "first alternate answer")],
)
async def test_reopen_preserves_selected_model_without_duplicate_history(
    client: TestClient, method: str, choice: str, answer: str
) -> None:
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
        rpc(
            socket,
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": choice},
        )
        assert answer in str(prompt(socket, session_id))
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        listing = rpc(socket, "session/list", {})
        assert session_id in str(listing)
        loaded = rpc(
            socket,
            method,
            {"sessionId": session_id, "cwd": "/another", "mcpServers": []},
        )
        assert choice in str(loaded)
        assert ("agent_message_chunk" in str(loaded)) == (method == "session/load")
        assert answer in str(prompt(socket, session_id))
    async with async_session() as session:
        assert len(await session.list(AgentRun)) == 2
        assert len(await session.list(ThreadMessage)) == 4


async def test_empty_session_reopens_and_attachment_is_exclusive(
    client: TestClient,
) -> None:
    with client.websocket_connect("/api/acp") as first:
        initialize(first)
        session_id = new_session(first)
        with client.websocket_connect("/api/acp") as second:
            initialize(second)
            result = rpc(
                second,
                "session/resume",
                {"sessionId": session_id, "cwd": "/", "mcpServers": []},
            )
            assert "already attached" in str(result)
            assert "not attached" in str(prompt(second, session_id))
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        assert (
            "error"
            not in rpc(
                socket,
                "session/resume",
                {"sessionId": session_id, "cwd": "/", "mcpServers": []},
            )[-1]
        )


async def test_foreign_account_cannot_load_list_or_prompt(client: TestClient) -> None:
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
    app = client.app
    assert isinstance(app, Octomate)
    assert app.auth is not None
    other = await a_user("bob")
    key = await app.auth.create_api_key(other.id, name="other", scopes=["acp"])
    with client.websocket_connect(
        "/api/acp", headers={"Authorization": f"Bearer {key.token.get_secret_value()}"}
    ) as socket:
        initialize(socket)
        assert session_id not in str(rpc(socket, "session/list", {}))
        assert "unavailable" in str(
            rpc(
                socket,
                "session/load",
                {"sessionId": session_id, "cwd": "/", "mcpServers": []},
            )
        )
        assert "not attached" in str(prompt(socket, session_id))


@pytest.mark.parametrize("path", ["/api/acp/claude", "/api/acp/codex"])
def test_provider_routes_are_removed(client: TestClient, path: str) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(path):
        pass


@pytest.mark.parametrize(
    "params",
    [
        {"cwd": "relative", "mcpServers": []},
        {"cwd": "/", "additionalDirectories": ["/extra"], "mcpServers": []},
        {
            "cwd": "/",
            "mcpServers": [
                {
                    "name": "remote",
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": [],
                }
            ],
        },
        {
            "cwd": "/",
            "mcpServers": [
                {
                    "name": "process",
                    "command": "/bin/echo",
                    "args": ["not-run"],
                    "env": [],
                }
            ],
        },
        {
            "cwd": "/",
            "mcpServers": [
                {
                    "name": "unsupported",
                    "type": "sse",
                    "url": "http://example.com",
                    "headers": [],
                }
            ],
        },
    ],
)
async def test_invalid_session_creates_no_thread(
    client: TestClient, params: dict[str, JsonValue]
) -> None:
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        assert "error" in rpc(socket, "session/new", params)[-1]
    async with async_session() as session:
        assert not await session.list(Thread)


@pytest.mark.parametrize("method", ["session/load", "session/resume"])
@pytest.mark.parametrize(
    "server",
    [
        {"name": "process", "command": "/bin/echo", "args": [], "env": []},
        {
            "name": "remote",
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": [],
        },
        {
            "name": "remote",
            "type": "sse",
            "url": "https://example.com/sse",
            "headers": [],
        },
    ],
)
async def test_reopen_refuses_mcp_without_attaching(
    client: TestClient, method: str, server: dict[str, JsonValue]
) -> None:
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        frames = rpc(
            socket,
            method,
            {
                "sessionId": session_id,
                "cwd": "/",
                "mcpServers": [server],
            },
        )
        assert "Session-supplied MCP servers are not supported" in str(frames)
        assert "not attached" in str(prompt(socket, session_id))


async def test_initialize_required_and_unknown_model_leaves_selection(
    client: TestClient,
) -> None:
    with client.websocket_connect("/api/acp") as socket:
        assert "Initialize first" in str(
            rpc(socket, "session/new", {"cwd": "/", "mcpServers": []})
        )
        initialize(socket)
        assert "Already initialized" in str(
            rpc(socket, "initialize", {"protocolVersion": 1})
        )
        session_id = new_session(socket)
        assert "Unknown configuration" in str(
            rpc(
                socket,
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "model",
                    "value": "missing:model",
                },
            )
        )
        assert "first answer" in str(prompt(socket, session_id))


@pytest.mark.parametrize("cancel", [False, True])
async def test_questions_resume_or_cancel_through_reflex(
    client: TestClient, cancel: bool
) -> None:
    app = client.app
    assert isinstance(app, Octomate)
    model = FunctionModel(
        stream_function=ScriptedStream(
            turns=[
                ScriptedTurn(
                    "ask_questions",
                    {
                        "questions": [
                            {"question": "Which color?", "choices": ["blue", "green"]}
                        ]
                    },
                    "ask-color",
                ),
                "Selected blue",
            ]
        )
    )
    app.connect(
        InklingTentacle(
            id="questions",
            octomate=app,
            models={"test": model},
            capabilities=[AskCapability()],
            gateway=False,
        )
    )
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket, elicitation=True)
        session_id = new_session(socket)
        rpc(
            socket,
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": "questions:test"},
        )
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "ask me"}],
                },
            }
        )
        while True:
            frame = socket.receive_json()
            assert "error" not in frame, frame
            assert frame.get("id") != "prompt", frame
            if frame.get("method") == "elicitation/create":
                break
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
                    "id": frame["id"],
                    "result": {"action": "accept", "content": {"answer": "blue"}},
                }
            )
        while True:
            frame = socket.receive_json()
            if frame.get("id") == "prompt":
                break
        assert frame["result"] == {"stopReason": "cancelled" if cancel else "end_turn"}
    assert not app.awakes


@pytest.mark.parametrize("decision", ["allow", "deny", "cancel", "disconnect"])
async def test_agent_permissions_and_cancellation(
    client: TestClient, decision: str
) -> None:
    app = client.app
    assert isinstance(app, Octomate)
    calls: list[str] = []

    def echo() -> str:
        """Return a fixture response after approval."""
        calls.append("echo")
        return "Tool answered"

    toolset = FunctionToolset[None]([echo], requires_approval=True)
    script = ScriptedStream(
        turns=[ScriptedTurn("echo", {}, "tool-call"), "Tool finished"]
    )

    async def reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        assert "echo" in {tool.name for tool in info.function_tools}
        assert info.instructions is not None
        assert "- tentacles: The user's installed MCP tools" in info.instructions
        async for chunk in script(messages, info):
            yield chunk

    model = FunctionModel(stream_function=reply)
    app.connect(
        InklingTentacle(
            id="tools",
            octomate=app,
            models={"test": model},
            capabilities=[Toolset(toolset)],
        )
    )
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
        rpc(
            socket,
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": "tools:test"},
        )
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "use the tool"}],
                },
            }
        )
        while True:
            frame = socket.receive_json()
            assert frame.get("id") != "prompt", frame
            if frame.get("method") == "session/request_permission":
                break
        if decision != "disconnect":
            # Configuration remains usable while a request waits for human input.
            selection = rpc(
                socket,
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": "second:test"},
            )
            assert "error" not in selection[-1]
            if decision == "cancel":
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
                        "id": frame["id"],
                        "result": {
                            "outcome": {"outcome": "selected", "optionId": decision}
                        },
                    }
                )
            while True:
                frame = socket.receive_json()
                if frame.get("id") == "prompt":
                    break
            assert frame["result"] == {
                "stopReason": "cancelled" if decision == "cancel" else "end_turn"
            }
            assert "second answer" in str(prompt(socket, session_id))
    assert calls == (["echo"] if decision == "allow" else [])
    assert not app.awakes
    bridge = app.tentacles["bridge"]
    assert isinstance(bridge, AcpChannelTentacle)
    assert not bridge.clients


async def test_permission_config_and_empty_model_catalog(client: TestClient) -> None:
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
        frames = rpc(
            socket,
            "session/set_config_option",
            {"sessionId": session_id, "configId": "mode", "value": "dontAsk"},
        )
        assert "dontAsk" in str(frames)
        assert "error" not in frames[-1]
        assert (
            "error"
            in rpc(
                socket,
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": "invalid"},
            )[-1]
        )
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        frames = rpc(
            socket,
            "session/resume",
            {"sessionId": session_id, "cwd": "/", "mcpServers": []},
        )
        assert "dontAsk" in str(frames)
    app = client.app
    assert isinstance(app, Octomate)
    for agent in app.agents.values():
        agent.models.clear()
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        assert "No agent models" in str(
            rpc(socket, "session/new", {"cwd": "/", "mcpServers": []})
        )


@pytest.mark.parametrize("frame", ["not json", "[]", "{}"])
def test_invalid_frames_close_the_connection(client: TestClient, frame: str) -> None:
    with client.websocket_connect("/api/acp") as socket:
        socket.send_text(frame)
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


async def test_workspace_filter_distinguishes_one_checkout_from_project(
    client: TestClient, tmp_path: Path
) -> None:
    app = client.app
    assert isinstance(app, Octomate)
    root = tmp_path / "project"
    root.mkdir()
    app.workspaces.projects = await a_registry(a_project(root, name="demo"))
    app.workspaces.workspaces_dir = root / ".octomate" / "workspaces"
    alias = tmp_path / "project-alias"
    alias.symlink_to(root, target_is_directory=True)
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        first = rpc(socket, "session/new", {"cwd": str(alias), "mcpServers": []})[-1][
            "result"
        ]
        second = rpc(socket, "session/new", {"cwd": str(root), "mcpServers": []})[-1][
            "result"
        ]
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert isinstance(first["sessionId"], str)
        checkout = app.workspaces.path(UUID(first["sessionId"]))
        workspace_alias = tmp_path / "workspace-alias"
        workspace_alias.symlink_to(checkout, target_is_directory=True)
        single = rpc(socket, "session/list", {"cwd": str(workspace_alias)})
        assert first["sessionId"] in str(single)
        assert str(second["sessionId"]) not in str(single)
        project = rpc(socket, "session/list", {"cwd": str(alias)})
        assert first["sessionId"] in str(project)
        assert str(second["sessionId"]) in str(project)


class ApprovingClaudeClient(FakeClaudeClient):
    async def receive_response(self) -> AsyncIterator[ClaudeMessage]:
        options = self.last_options
        assert isinstance(options, ClaudeAgentOptions)
        assert options.can_use_tool is not None
        await options.can_use_tool(
            "Bash",
            {"command": "echo fixture"},
            ToolPermissionContext(tool_use_id="t1", description="Run fixture command"),
        )
        async for event in super().receive_response():
            yield event


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_native_live_approval_returns_to_existing_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    app = client.app
    assert isinstance(app, Octomate)
    monkeypatch.setattr(claude_ink, "ClaudeSDKClient", ApprovingClaudeClient)
    agent = app.connect(
        ClaudeCodeTentacle(
            "native",
            app,
            config=ClaudeCodeConfig(gateway=False),
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
    agent.models = {"anthropic:sonnet": "sonnet"}
    with client.websocket_connect("/api/acp") as socket:
        initialize(socket)
        session_id = new_session(socket)
        rpc(
            socket,
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": "model",
                "value": "native:anthropic:sonnet",
            },
        )
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "hello"}],
                },
            }
        )
        while True:
            frame = socket.receive_json()
            assert frame.get("id") != "prompt", frame
            if frame.get("method") == "session/request_permission":
                break
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": decision}},
            }
        )
        while True:
            frame = socket.receive_json()
            if frame.get("id") == "prompt":
                break
        assert frame["result"] == {"stopReason": "end_turn"}
    async with async_session() as session:
        assert len(await session.list(AgentRun)) == 1
    assert not app.awakes
    assert not agent.pendings
