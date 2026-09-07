from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig, ZcodeConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.zcode import ZcodeTentacle
from octomate.tentacles.zcode import base as zcode_base
from octomate.tentacles.zcode.wire import DesktopConfig
from octomate.types.permissions import check_mode
from tests.support.managers import FakeConversationManager, a_thread
from tests.support.zcode import FakeZcodeClient, desktop_config

ADDRESS = ChannelAddress(
    channel_tentacle_id="test", chat_type="dm", chat_id="alice", user_id="alice"
)


@pytest.fixture
def tentacle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ZcodeTentacle:
    monkeypatch.setattr(zcode_base, "ZcodeClient", FakeZcodeClient)
    monkeypatch.setattr(FakeZcodeClient, "instances", [])
    monkeypatch.setattr(FakeZcodeClient, "history", {})
    monkeypatch.setattr(FakeZcodeClient, "prompted", asyncio.Event())
    return ZcodeTentacle(
        "zcode",
        Octomate(conversations=FakeConversationManager()),
        config=desktop_config(tmp_path),
    )


def test_desktop_provider_translation_and_redaction(tmp_path: Path) -> None:
    config = desktop_config(tmp_path)
    source = config.desktop_config.read_bytes()
    desktop = DesktopConfig.read(config.desktop_config)
    assert "secret-test-key" not in repr(desktop)
    runtime = desktop.runtime_model("builtin:bigmodel", "GLM-5.3", "low")
    assert runtime["thoughtLevel"] == "low"
    provider = runtime["provider"]
    assert isinstance(provider, dict)
    assert provider["apiKey"] == {"source": "inline", "value": "secret-test-key"}
    models = provider["models"]
    assert isinstance(models, list)
    model = models[0]
    assert isinstance(model, dict)
    assert model["contextWindow"] == 1000000
    assert model["maxOutputTokens"] == 128000
    assert model["supportsImages"] is True
    assert config.desktop_config.read_bytes() == source
    assert (
        desktop.runtime_model("builtin:bigmodel", "GLM-5.3", None)["thoughtLevel"]
        == "max"
    )
    with pytest.raises(ValueError, match="missing or disabled"):
        desktop.runtime_model("missing", "GLM-5.3", None)
    with pytest.raises(ValueError, match="has no model"):
        desktop.runtime_model("builtin:bigmodel", "GLM-5.2", None)
    desktop.provider["builtin:bigmodel"].options.api_key = SecretStr("")
    with pytest.raises(ValueError, match="desktop-login"):
        desktop.runtime_model("builtin:bigmodel", "GLM-5.3", None)


def test_config_routes_and_permission_registration() -> None:
    config = OctomateConfig.model_validate(
        {
            "agents": {"zcode": {"models": ["GLM-5.3"]}},
            "channels": {
                "slack": {
                    "type": "slack",
                    "app_id": "A-test",
                    "bot_token": "test",
                    "app_token": "test",
                    "agents": [{"agent": "zcode", "model": "GLM-5.3"}],
                }
            },
        }
    )
    assert config.agents.configured_models()["zcode"] == {"GLM-5.3"}
    assert config.agents.zcode is not None
    assert config.agents.zcode.permission_mode == "build"
    assert config.agents.zcode.gateway is False
    check_mode("zcode", "build")
    with pytest.raises(ValueError, match="not one"):
        check_mode("zcode", "user_review")
    with pytest.raises(ValidationError):
        ZcodeConfig.model_validate({"models": ["GLM-5.3"], "gateway": True})
    with pytest.raises(ValidationError):
        ZcodeConfig.model_validate({"models": []})
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate(
            {
                "agents": {"zcode": {"models": ["GLM-5.3"]}},
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "test",
                        "app_token": "test",
                        "agents": [{"agent": "zcode", "model": "GLM-5.3-Flash"}],
                    }
                },
            }
        )


async def test_stream_resume_and_history_boundaries(tentacle: ZcodeTentacle) -> None:
    thread_id = uuid7()
    async with tentacle.run_stream_events(
        "first", conversation_address=ADDRESS, thread_id=thread_id
    ) as stream:
        events = [event async for event in stream]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "canonical first"
    result = await tentacle.run(
        "second", conversation_address=ADDRESS, thread_id=thread_id
    )
    assert result.output == "canonical second"
    assert result.usage.input_tokens == 10
    first, second = FakeZcodeClient.instances
    assert first is not second
    assert first.closed
    assert second.closed
    assert first.session_id == second.session_id
    assert second.calls[0][0] == "session/resume"
    assert second.calls[1] == (
        "session/setMode",
        {"sessionId": second.session_id, "mode": "build"},
    )
    assert [name for name, _ in second.calls].index("session/subscribe") < [
        name for name, _ in second.calls
    ].index("session/send")
    manager = tentacle.octomate.conversations
    assert isinstance(manager, FakeConversationManager)
    assert len(manager.runs) == 2
    assert len(manager.runs[1][2]) == 2
    response = manager.runs[1][2][1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("canonical second")]
    assert "secret-test-key" not in repr(manager.runs)


async def test_partial_failure_and_cancellation_persist_once(
    tentacle: ZcodeTentacle,
) -> None:
    with pytest.raises(AgentRunError, match="broken pipe"):
        await tentacle.run("fail", conversation_address=ADDRESS, thread_id=uuid7())
    async with tentacle.run_stream_events(
        "hang", conversation_address=ADDRESS, thread_id=uuid7()
    ) as stream:
        async for _ in stream:
            break
    assert FakeZcodeClient.instances[-1].closed
    assert "session/stop" in [
        method for method, _ in FakeZcodeClient.instances[-1].calls
    ]
    manager = tentacle.octomate.conversations
    assert isinstance(manager, FakeConversationManager)
    assert len(manager.runs) == 2
    assert all(run[0].external_id for run in manager.runs)


async def test_task_cancellation_releases_conversation(tentacle: ZcodeTentacle) -> None:
    thread_id = uuid7()
    running = asyncio.create_task(
        tentacle.run("hang", conversation_address=ADDRESS, thread_id=thread_id)
    )
    assert FakeZcodeClient.prompted is not None
    await FakeZcodeClient.prompted.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert FakeZcodeClient.instances[0].closed
    result = await tentacle.run(
        "after", conversation_address=ADDRESS, thread_id=thread_id
    )
    assert result.output == "canonical after"
    assert (
        FakeZcodeClient.instances[1].session_id
        == FakeZcodeClient.instances[0].session_id
    )


async def test_conversations_are_isolated_and_foreign_ids_rejected(
    tentacle: ZcodeTentacle,
) -> None:
    one, two = uuid7(), uuid7()
    await asyncio.gather(
        tentacle.run("one", conversation_address=ADDRESS, thread_id=one),
        tentacle.run("two", conversation_address=ADDRESS, thread_id=two),
    )
    first, second = FakeZcodeClient.instances
    assert first.session_id != second.session_id
    conversation = await tentacle.octomate.conversations.ensure(
        one, agent_tentacle_id="zcode"
    )
    with pytest.raises(ValueError, match="does not belong"):
        await tentacle.run(
            "wrong",
            conversation_address=ADDRESS,
            thread_id=two,
            conversation_id=conversation.id,
        )


async def test_resume_uses_the_current_workspace(
    tentacle: ZcodeTentacle,
    tmp_path: Path,
) -> None:
    thread_id = uuid7()
    await tentacle.run("before", conversation_address=ADDRESS, thread_id=thread_id)
    tentacle.octomate.workspaces.workspaces_dir = tmp_path / "relocated"
    await tentacle.run("after", conversation_address=ADDRESS, thread_id=thread_id)
    first, second = FakeZcodeClient.instances
    assert first.session_id == second.session_id
    assert first.cwd != second.cwd
    assert second.cwd.is_relative_to(tmp_path / "relocated")
    assert second.calls[0][1]["workspace"] == {
        "workspacePath": str(second.cwd),
        "workspaceKey": str(second.cwd),
    }
    manager = tentacle.octomate.conversations
    assert isinstance(manager, FakeConversationManager)
    assert manager.runs[-1][0].runs[-1].cwd == second.cwd


async def test_instructions_and_unsupported_inputs(tentacle: ZcodeTentacle) -> None:
    await tentacle.run(
        "prompt",
        conversation_address=ADDRESS,
        thread_id=uuid7(),
        instructions="framing",
        effort="high",
    )
    calls = dict(FakeZcodeClient.instances[0].calls)
    assert "framing" in str(calls["session/send"]["content"])
    runtime = calls["session/create"]["runtimeModel"]
    assert isinstance(runtime, dict)
    assert runtime["thoughtLevel"] == "high"
    with pytest.raises(ValueError, match="text output"):
        await tentacle.run(
            "prompt", conversation_address=ADDRESS, thread_id=uuid7(), output_type=int
        )
    with pytest.raises(ValueError, match="text input"):
        await tentacle.run(
            [BinaryContent(data=b"png", media_type="image/png")],
            conversation_address=ADDRESS,
            thread_id=uuid7(),
        )
    assert len(FakeZcodeClient.instances) == 1


async def test_records_with_real_managers_and_resumes_after_reload(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zcode_base, "ZcodeClient", FakeZcodeClient)
    monkeypatch.setattr(FakeZcodeClient, "instances", [])
    monkeypatch.setattr(FakeZcodeClient, "history", {})
    thread_id = await a_thread()
    agent = ZcodeTentacle("zcode", Octomate(), config=desktop_config(tmp_path))
    first = await agent.run("first", conversation_address=ADDRESS, thread_id=thread_id)
    second = await agent.run(
        "second", conversation_address=ADDRESS, thread_id=thread_id
    )
    assert first.output == "canonical first"
    assert second.output == "canonical second"
    conversation = await agent.octomate.conversations.ensure(
        thread_id, agent_tentacle_id="zcode"
    )
    assert conversation.external_id == FakeZcodeClient.instances[0].session_id
    assert (
        FakeZcodeClient.instances[0].session_id
        == FakeZcodeClient.instances[1].session_id
    )
