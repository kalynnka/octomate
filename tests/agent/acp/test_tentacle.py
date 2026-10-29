"""Outbound ACP runs use ordinary threads, reflex dispatch, and deferred actions."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import acp
import pytest
from anyio import Path as AsyncPath
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    ReflexAsleepEvent,
    RunEndedEvent,
    RunStartedEvent,
)
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.mcp.gateway import CONVERSATION_HEADER
from octomate.nerve import ThreadTopic
from octomate.reflex.state import ReflexResult
from octomate.schemas.auth import UserApiKey
from octomate.schemas.awakes import DeferredActionBatchResponse, UserMessageSignal
from octomate.schemas.conversation import Conversation
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelResponse
from octomate.schemas.runs import AgentRun
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import MessageBinding, Thread, ThreadKey, ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.acp.channel import AcpChannelTentacle
from octomate.tentacles.acp.client import AcpProvider
from tests.agent.acp.test_client import adapter_environment as adapter_environment
from tests.agent.acp.test_routes import initialize, new_session, rpc
from tests.support.managers import a_project, a_registry
from tests.support.nerve import held
from tests.support.users import a_user, auth_config


@pytest.fixture
def custom_provider(tmp_path: Path) -> AcpProvider:
    return AcpProvider.model_validate(
        {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("fixtures") / "acp_agent.py")],
            "env": {
                "ACP_TEST_LOG": str(tmp_path / "custom-requests.jsonl"),
                "ACP_TEST_MODELS": "grouped",
                "ACP_TEST_TEXT": "custom harness answer",
            },
        }
    )


@pytest.mark.parametrize("supports_close", [False, True])
async def test_catalog_discovery_closes_its_temporary_session_when_supported(
    custom_provider: AcpProvider,
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    supports_close: bool,
) -> None:
    provider = AcpProvider.model_validate(
        {
            **custom_provider.model_dump(),
            "env": {
                **custom_provider.env,
                "ACP_TEST_CLOSE": "1" if supports_close else "0",
            },
        }
    )
    agent = AcpAgentTentacle("custom", Octomate(), provider=provider)
    async with agent:
        assert set(agent.models) == {"fast", "careful"}
        messages = [
            json.loads(line)["message"]
            for line in (tmp_path / "custom-requests.jsonl").read_text().splitlines()
        ]
        closing = [one for one in messages if one["method"] == "session/close"]
        assert len(closing) == int(supports_close)
        if supports_close:
            assert messages[-1] == closing[0]
            assert closing[0]["params"] == {"sessionId": "native-1"}


@pytest.mark.parametrize("catalog", ["grouped", "flat", "none"])
async def test_custom_harness_discovers_selects_and_reopens(
    custom_provider: AcpProvider,
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    catalog: str,
) -> None:
    provider = AcpProvider.model_validate(
        {
            **custom_provider.model_dump(),
            "env": {
                **custom_provider.env,
                "ACP_TEST_MODELS": "" if catalog == "none" else catalog,
                "ACP_TEST_USAGE": "1",
            },
        }
    )
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = app.connect(AcpAgentTentacle("custom", app, provider=provider))
    user = await a_user("alice")
    sender = await app.users.native_profile(agent.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(agent.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(agent.id, "thread", str(user.id), uuid7().hex)
    )
    async with agent:
        assert set(agent.models) == (
            {"default"} if catalog == "none" else {"fast", "careful"}
        )
        assert agent.default_model == (None if catalog == "none" else "fast")
        assert not agent.routers()
        async with async_session() as session:
            assert not await session.list(Conversation)
            assert not await session.list(AgentRun)
        for model in ["default", None] if catalog == "none" else ["careful", "fast"]:
            result = await agent.run(
                "hello",
                conversation_address=thread.key.address(sender.channel_user_id),
                thread_id=thread.id,
                model=model,
                interactive=False,
            )
            assert result.output == "custom harness answer"
        with pytest.raises(ValueError, match="does not serve model"):
            agent.resolve_model("unknown")
    async with async_session() as session:
        [conversation] = await session.list(Conversation)
        runs = await session.list(AgentRun)
        assert len(runs) == 2
        assert {run.conversation_id for run in runs} == {conversation.id}
        assert [run.model_name for run in runs] == (
            [None, None] if catalog == "none" else ["careful", "fast"]
        )
        assert all(run.native_id == agent.id for run in runs)
        for run in runs:
            assert run.context_used == 32000
            assert run.context_window == 128000
            await run.messages
            response = run.messages[-1]
            assert isinstance(response, ModelResponse)
            assert response.usage.input_tokens == 0
            assert response.metadata is not None
            assert response.metadata["usage"] == {
                "inputTokens": 9999,
                "outputTokens": 1,
                "totalTokens": 10000,
            }
        assert all(key.revoked_at is not None for key in await session.list(UserApiKey))
    messages = [
        json.loads(line)["message"]
        for line in (tmp_path / "custom-requests.jsonl").read_text().splitlines()
    ]
    assert messages[1]["method"] == "session/load"
    assert "replayed history" not in result.output
    assert not agent.routes


@pytest.mark.parametrize("catalog", ["grouped", "flat"])
async def test_harness_efforts_follow_model_and_reach_new_and_loaded_sessions(
    custom_provider: AcpProvider,
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    catalog: str,
) -> None:
    provider = AcpProvider.model_validate(
        {
            **custom_provider.model_dump(),
            "env": {**custom_provider.env, "ACP_TEST_EFFORTS": catalog},
        }
    )
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = app.connect(AcpAgentTentacle("custom", app, provider=provider))
    user = await a_user("alice")
    sender = await app.users.native_profile(agent.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(agent.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(agent.id, "thread", str(user.id), uuid7().hex)
    )
    conversation = await agent.resolve_conversation(
        thread.id, conversation_address=thread.key.address(sender.channel_user_id)
    )
    async with agent:
        assert {route.model: route.claim.efforts for route in agent.info.routes} == {
            "fast": ("low", "medium", "high"),
            "careful": ("high", "max"),
        }
        assert agent.default_model == "fast"
        for model, explicit, stored, expected in [
            ("careful", "max", "high", "max"),
            ("fast", None, "low", "low"),
            (None, "medium", "low", None),
            ("fast", None, None, None),
        ]:
            await app.conversations.set_effort(conversation, stored)
            result = await agent.run(
                "hello",
                conversation_address=thread.key.address(sender.channel_user_id),
                thread_id=thread.id,
                model=model,
                effort=explicit,
                interactive=False,
            )
            assert result.output == "custom harness answer"
            requests = [
                json.loads(line)["message"]
                for line in (tmp_path / "custom-requests.jsonl")
                .read_text()
                .splitlines()
            ]
            settings = [
                item["params"]
                for item in requests
                if item.get("method") == "session/set_config_option"
            ]
            assert [
                item["value"] for item in settings if item["configId"] == "deliberation"
            ] == ([] if expected is None else [expected])
            if model == "careful":
                assert [item["configId"] for item in settings] == [
                    "engine",
                    "deliberation",
                ]
            assert requests[1]["method"] == (
                "session/new" if model == "careful" else "session/load"
            )
        with pytest.raises(ValueError, match="does not take effort"):
            await agent.run(
                "hello",
                conversation_address=thread.key.address(sender.channel_user_id),
                thread_id=thread.id,
                model="fast",
                effort="max",
                interactive=False,
            )


async def test_public_endpoint_drives_custom_harness(
    custom_provider: AcpProvider,
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = app.connect(AcpAgentTentacle("custom", app, provider=custom_provider))
    app.connect(AcpChannelTentacle("entry", app))
    user = await a_user("alice")
    assert app.auth is not None
    key = await app.auth.create_api_key(user.id, name="test", scopes=["acp"])
    client = TestClient(
        app, headers={"Authorization": f"Bearer {key.token.get_secret_value()}"}
    )
    async with agent:
        with client.websocket_connect("/api/acp") as socket:
            initialize(socket)
            session_id = new_session(socket)
            frames = rpc(
                socket,
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "model",
                    "value": "custom:careful",
                },
            )
            assert "result" in frames[-1]
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
            while (frame := socket.receive_json()).get(
                "method"
            ) != "session/request_permission":
                assert frame.get("id") != "prompt", frame
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "allow"}},
                }
            )
            while (frame := socket.receive_json()).get("id") != "prompt":
                pass
            assert frame["result"] == {"stopReason": "end_turn"}
    async with async_session() as session:
        [conversation] = await session.list(Conversation)
        [run] = await session.list(AgentRun)
        assert str(conversation.thread_id) == session_id
        assert conversation.agent_tentacle_id == "custom"
        assert run.model_name == "careful"
        assert len(await session.list(ThreadMessage)) == 2
    assert not app.awakes
    assert not agent.pendings


@pytest.mark.parametrize("gateway", [True, False])
async def test_harness_without_http_mcp(
    custom_provider: AcpProvider,
    in_memory_engine: AsyncEngine,
    gateway: bool,
) -> None:
    provider = AcpProvider.model_validate(
        {
            **custom_provider.model_dump(),
            "env": {**custom_provider.env, "ACP_TEST_HTTP": "0"},
        }
    )
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    agent = app.connect(
        AcpAgentTentacle("custom", app, provider=provider, gateway=gateway)
    )
    user = await a_user("alice")
    sender = await app.users.native_profile(agent.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(agent.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(agent.id, "thread", str(user.id), uuid7().hex)
    )
    async with agent:
        if gateway:
            with pytest.raises(ValueError, match="must support HTTP MCP"):
                await agent.run(
                    "hello",
                    thread_id=thread.id,
                    conversation_address=thread.key.address(sender.channel_user_id),
                )
            return
        result = await agent.run(
            "hello",
            thread_id=thread.id,
            conversation_address=thread.key.address(sender.channel_user_id),
            interactive=False,
        )
        assert result.output == "custom harness answer"
    async with async_session() as session:
        assert not await session.list(UserApiKey)


@pytest.fixture(params=["claude-agent-acp", "codex-acp"])
async def agent(
    request: pytest.FixtureRequest,
    adapter_environment: dict[str, str],
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> tuple[AcpAgentTentacle, UserProfile, Thread]:
    provider = AcpProvider(command=request.param)
    executable = tmp_path / provider.command
    source = executable.read_text()
    executable.write_text(
        source.replace(
            'os.environ["ACP_TEST_LOG"]', repr(adapter_environment["ACP_TEST_LOG"])
        )
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("ACP test workspace\n")
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    app.workspaces.projects = await a_registry(a_project(root, name="project"))
    tentacle = app.connect(AcpAgentTentacle(id="acp", octomate=app, provider=provider))
    user = await a_user("alice")
    sender = await app.users.native_profile(tentacle.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(tentacle.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(tentacle.id, "thread", str(user.id), uuid7().hex),
        project=app.projects.get("project"),
    )
    return tentacle, sender, thread


@pytest.mark.parametrize("allowed", [True, False])
async def test_reflex_owns_dispatch_and_history(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    adapter_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
) -> None:
    tentacle, sender, thread = agent
    app = tentacle.octomate
    kick = AsyncMock(wraps=app.kick)
    monkeypatch.setattr(app, "kick", kick)
    message = MessageEvent(
        tentacle_id=tentacle.id,
        chat_type="thread",
        chat_id=thread.chat_id,
        channel_thread_id=thread.channel_thread_id,
        user_id=sender.channel_user_id,
        sender=sender,
        message_id=uuid7().hex,
        segments=[TextSegment(data={"text": "hello from the reflex"})],
    )
    inbound = await app.threads.record_inbound(message)
    with app.nerve.subscribe(ThreadTopic(thread.key)) as stream:
        task = await app.kick(
            UserMessageSignal(
                [message], agent_id=tentacle.id, trigger_thread_message_id=inbound.id
            )
        )
        assert task is not None
        seen = []
        async for impulse in stream:
            seen.append(impulse.event)
            if isinstance(impulse.event, ActionBatchEvent):
                batch = impulse.event
                response = DeferredActionBatchResponse(
                    batch_id=UUID(batch.batch_id),
                    responder_id=sender.channel_user_id,
                    approvals={batch.approvals[0].id: allowed},
                )
                assert await app.kick(response) is None
            if isinstance(impulse.event, ReflexAsleepEvent):
                break
        result = await task
    assert isinstance(result, ReflexResult)
    assert result.result is not None
    assert result.result.output == "x" * 100_000
    assert kick.await_count == 2  # The prompt and its live approval; no broker kick.
    assert sum(isinstance(event, RunStartedEvent) for event in seen) == 1
    assert sum(isinstance(event, RunEndedEvent) for event in seen) == 1
    assert not app.awakes
    assert not tentacle.pendings
    assert not tentacle.driven_sessions
    async with async_session() as session:
        [stored_thread] = await session.list(Thread)
        [conversation] = await session.list(Conversation)
        [run] = await session.list(AgentRun)
        assert stored_thread.id == thread.id
        assert conversation.thread_id == thread.id
        assert conversation.agent_tentacle_id == tentacle.id
        assert conversation.external_id == "native-1"
        assert run.conversation_id == conversation.id
        assert len(run.messages) == 2
        assert len(await session.list(ThreadMessage)) == 2
        bindings = await session.list(MessageBinding)
        assert {binding.kind for binding in bindings} == {
            "request_source",
            "assistant_reply",
        }
        assert all(binding.run_id == run.id for binding in bindings)
        [key] = await session.list(UserApiKey)
        assert key.revoked_at is not None
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    messages = [json.loads(line)["message"] for line in log.splitlines()]
    initialize = messages[0]["params"]
    assert not initialize.get("clientCapabilities", {}).get("terminal", False)
    new = next(
        item["params"] for item in messages if item.get("method") == "session/new"
    )
    assert new["cwd"] == str(app.workspaces.path(thread.id))
    [mcp] = new["mcpServers"]
    headers = {item["name"]: item["value"] for item in mcp["headers"]}
    assert headers[CONVERSATION_HEADER] == str(conversation.id)
    token = headers["Authorization"].removeprefix("Bearer ")
    assert await app.bearers.owner(token, scope="mcp") is None
    prompt = next(
        item["params"] for item in messages if item.get("method") == "session/prompt"
    )
    assert "hello from the reflex" in prompt["prompt"][0]["text"]
    reply = next(item for item in messages if item.get("id") == "permission")
    assert reply["result"]["outcome"] == (
        {"outcome": "selected", "optionId": "yes"}
        if allowed
        else {"outcome": "cancelled"}
    )


async def test_direct_runs_reopen_the_conversation_without_dispatch(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    adapter_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tentacle, sender, thread = agent
    app = tentacle.octomate
    kick = AsyncMock()
    monkeypatch.setattr(app, "kick", kick)
    for prompt in ("first", "second"):
        result = await tentacle.run(
            prompt,
            thread_id=thread.id,
            conversation_address=thread.key.address(sender.channel_user_id),
            interactive=False,
        )
        assert result.output == "x" * 100_000
    kick.assert_not_called()
    async with async_session() as session:
        assert len(await session.list(Thread)) == 1
        [conversation] = await session.list(Conversation)
        assert conversation.external_id == "native-1"
        assert len(await session.list(AgentRun)) == 2
        assert not await session.list(ThreadMessage)
        assert all(key.revoked_at is not None for key in await session.list(UserApiKey))
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    messages = [json.loads(line)["message"] for line in log.splitlines()]
    assert [item.get("method") for item in messages[:3]] == [
        "initialize",
        "session/load",
        "session/prompt",
    ]
    assert messages[1]["params"]["sessionId"] == "native-1"


async def test_cancel_settles_provider_and_keeps_partial_history(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    adapter_environment: dict[str, str],
) -> None:
    tentacle, sender, thread = agent
    app = tentacle.octomate
    message = MessageEvent(
        tentacle_id=tentacle.id,
        chat_type="thread",
        chat_id=thread.chat_id,
        channel_thread_id=thread.channel_thread_id,
        user_id=sender.channel_user_id,
        sender=sender,
        message_id=uuid7().hex,
        segments=[TextSegment(data={"text": "wait for approval"})],
    )
    inbound = await app.threads.record_inbound(message)
    with app.nerve.subscribe(ThreadTopic(thread.key)) as stream:
        task = await app.kick(
            UserMessageSignal(
                [message], agent_id=tentacle.id, trigger_thread_message_id=inbound.id
            )
        )
        assert task is not None
        async for impulse in stream:
            if isinstance(impulse.event, ActionBatchEvent):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(isinstance(item.event, ReflexAsleepEvent) for item in held(stream))
    assert not app.awakes
    assert not tentacle.pendings
    assert not tentacle.driven_sessions
    async with async_session() as session:
        [run] = await session.list(AgentRun)
        assert run.messages[0].metadata is not None
        assert run.messages[0].metadata["stop_reason"] == "cancelled"
        [batch] = await session.list(DeferredActionBatch)
        assert batch.status == "expired"
        assert len(run.messages) == 2
        assert len(await session.list(ThreadMessage)) == 2
        assert all(key.revoked_at is not None for key in await session.list(UserApiKey))
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    assert any(
        json.loads(line)["message"].get("method") == "session/cancel"
        for line in log.splitlines()
    )


async def test_conversation_cannot_cross_threads(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    adapter_environment: dict[str, str],
) -> None:
    tentacle, sender, thread = agent
    other = await tentacle.threads.ensure(
        ThreadKey(tentacle.id, "thread", "other", uuid7().hex)
    )
    conversation = await tentacle.conversations.ensure(
        other.id, agent_tentacle_id=tentacle.id
    )
    with pytest.raises(ValueError, match="does not belong"):
        await tentacle.run(
            "wrong thread",
            thread_id=thread.id,
            conversation_address=thread.key.address(sender.channel_user_id),
            conversation_id=conversation.id,
        )
    assert not await AsyncPath(adapter_environment["ACP_TEST_LOG"]).exists()


async def test_provider_failure_preserves_partial_run_and_revokes_key(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    tmp_path: Path,
) -> None:
    tentacle, sender, thread = agent
    executable = AsyncPath(tmp_path / tentacle.provider.command)
    source = await executable.read_text()
    await executable.write_text(
        source.replace(
            'send({"id": pending, "result": result})',
            'send({"id": pending, "error": {"code": -32603, "message": "provider failed"}})',
        )
    )
    with pytest.raises(acp.RequestError, match="provider failed"):
        await tentacle.run(
            "fail after a partial answer",
            thread_id=thread.id,
            conversation_address=thread.key.address(sender.channel_user_id),
            interactive=False,
        )
    assert not tentacle.driven_sessions
    assert not tentacle.pendings
    async with async_session() as session:
        [run] = await session.list(AgentRun)
        assert run.messages[0].metadata is not None
        error = run.messages[0].metadata["error"]
        assert isinstance(error, str)
        assert "provider failed" in error
        assert len(run.messages) == 2
        [partial] = await session.list(ThreadMessage)
        assert partial.direction == "outbound"
        [binding] = await session.list(MessageBinding)
        assert binding.kind == "assistant_reply"
        assert binding.run_id == run.id
        assert all(key.revoked_at is not None for key in await session.list(UserApiKey))
