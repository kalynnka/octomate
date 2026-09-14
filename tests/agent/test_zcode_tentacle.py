from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig, ZcodeConfig
from octomate.config.agents.common import Claim
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.zcode import ZcodeTentacle
from octomate.tentacles.zcode import base as zcode_base
from octomate.tentacles.zcode.wire import DesktopConfig, StateUpdated
from octomate.types.json import JsonObject, JsonValue
from octomate.types.permissions import check_mode
from tests.support.managers import FakeConversationManager, a_thread
from tests.support.zcode import (
    INTERACTIVE_SERVER,
    FakeZcodeClient,
    desktop_config,
    event,
    history_message,
)

ADDRESS = ChannelAddress(
    channel_tentacle_id="test", chat_type="dm", chat_id="alice", user_id="alice"
)


@pytest.fixture
async def tentacle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ZcodeTentacle:
    monkeypatch.setattr(zcode_base, "ZcodeClient", FakeZcodeClient)
    monkeypatch.setattr(FakeZcodeClient, "instances", [])
    monkeypatch.setattr(FakeZcodeClient, "history", {})
    monkeypatch.setattr(FakeZcodeClient, "prompted", asyncio.Event())
    agent = ZcodeTentacle(
        "zcode",
        Octomate(conversations=FakeConversationManager()),
        config=desktop_config(tmp_path),
    )
    await agent.discover_models()
    FakeZcodeClient.instances.clear()
    return agent


def test_desktop_provider_translation_and_redaction(tmp_path: Path) -> None:
    config = desktop_config(tmp_path)
    source = config.desktop_config.read_bytes()
    desktop = DesktopConfig.read(config.desktop_config)
    assert "secret-test-key" not in repr(desktop)
    runtime = desktop.runtime_model("builtin:bigmodel", "GLM-5.3", "low").model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
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
        desktop.runtime_model("builtin:bigmodel", "GLM-5.3", None).thought_level
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
            "agents": {"zcode": {}},
            "channels": {
                "slack": {
                    "type": "slack",
                    "app_id": "A-test",
                    "bot_token": "test",
                    "app_token": "test",
                    "agents": ["zcode"],
                }
            },
        }
    )
    assert [agent.id for agent in config.agents.configured_agents] == ["zcode"]
    assert config.agents.zcode is not None
    assert config.agents.zcode.permission_mode == "build"
    assert config.agents.zcode.approval_timeout == 3600
    assert ZcodeConfig(approval_timeout=None).approval_timeout is None
    assert config.agents.zcode.gateway is False
    check_mode("zcode", "build")
    with pytest.raises(ValueError, match="not one"):
        check_mode("zcode", "user_review")
    with pytest.raises(ValidationError):
        ZcodeConfig.model_validate({"gateway": True})
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate(
            {
                "agents": {"zcode": {}},
                "channels": {
                    "slack": {
                        "type": "slack",
                        "app_id": "A-test",
                        "bot_token": "test",
                        "app_token": "test",
                        "agents": ["missing"],
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
    await agent.discover_models()
    FakeZcodeClient.instances.clear()
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


class LifecycleClient(FakeZcodeClient):
    scenario: ClassVar[str] = ""

    async def call(self, method: str, params: JsonObject) -> JsonValue:
        if method == self.scenario:
            raise AgentRunError("setup rejected")
        if method == "session/send" and self.scenario in {
            "preflight",
            "blocked",
            "control",
        }:
            input_id = params["inputId"]
            assert isinstance(input_id, str)
            if self.scenario == "preflight":
                self.events.put_nowait(
                    StateUpdated(
                        type="state.updated",
                        scope="session",
                        session_id=self.session_id,
                        revision=11,
                        reason="prompt_failed",
                    )
                )
            else:
                for frame in [
                    event(
                        "turn.started",
                        {
                            "inputId": input_id,
                            **(
                                {"messageId": "blocked"}
                                if self.scenario == "blocked"
                                else {"executionKind": "controlOnly"}
                            ),
                        },
                    ),
                    event(
                        "turn.completed",
                        {"resultType": "success", "response": self.scenario},
                    ),
                ]:
                    frame.session_id = self.session_id
                    self.events.put_nowait(frame)
            return {"accepted": True, "sessionId": self.session_id, "stateRevision": 10}
        if method == "session/send" and self.scenario == "stale_failure":
            self.events.put_nowait(
                StateUpdated(
                    type="state.updated",
                    scope="session",
                    session_id=self.session_id,
                    revision=1,
                    reason="prompt_failed",
                )
            )
        return await super().call(method, params)


@pytest.mark.parametrize(
    "failure", ["session/setMode", "session/subscribe", "session/send", "preflight"]
)
async def test_rejected_input_does_not_record_or_consume_source_messages(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(zcode_base, "ZcodeClient", LifecycleClient)
    monkeypatch.setattr(LifecycleClient, "scenario", failure)
    host = Octomate()
    source = await host.thread_manager.record_inbound(
        MessageEvent(
            tentacle_id="test",
            message_id="source",
            chat_type="dm",
            chat_id="alice",
            user_id="alice",
            sender=UserProfile(channel_user_id="alice", name="Alice"),
            segments=[TextSegment(data={"text": "unsent"})],
        )
    )
    thread = await host.thread_manager.ensure(ADDRESS)
    agent = ZcodeTentacle("zcode", host, config=desktop_config(tmp_path))
    async with asyncio.timeout(2):
        with pytest.raises(AgentRunError, match=r"setup rejected|prompt_failed"):
            await agent.run(
                "unsent",
                conversation_address=ADDRESS,
                thread_id=thread.id,
                source_thread_message_ids=[source.id],
            )
    stored = await host.thread_manager.ensure(ADDRESS)
    conversation = await host.conversations.ensure(thread.id, agent_tentacle_id="zcode")
    assert stored.source_cursor_message_id is None
    assert not await conversation.runs
    assert conversation.external_id is None
    assert not agent.live_clients
    assert LifecycleClient.instances[-1].closed


@pytest.mark.parametrize("scenario", ["blocked", "control", "stale_failure"])
async def test_no_prompt_turns_and_old_failures_do_not_break_resumed_sessions(
    tentacle: ZcodeTentacle,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    thread_id = uuid7()
    await tentacle.run("first", conversation_address=ADDRESS, thread_id=thread_id)
    monkeypatch.setattr(zcode_base, "ZcodeClient", LifecycleClient)
    monkeypatch.setattr(LifecycleClient, "scenario", scenario)
    async with asyncio.timeout(2):
        result = await tentacle.run(
            "next", conversation_address=ADDRESS, thread_id=thread_id
        )
    assert result.output == (
        "canonical next" if scenario == "stale_failure" else scenario
    )
    manager = tentacle.octomate.conversations
    assert isinstance(manager, FakeConversationManager)
    assert len(manager.runs) == 2
    assert "canonical first" not in str(manager.runs[-1][2])
    assert len(manager.runs[-1][2]) == 2


async def test_history_reads_only_the_tail_before_send_and_current_turn_afterwards(
    tentacle: ZcodeTentacle,
) -> None:
    thread_id = uuid7()
    await tentacle.run("first", conversation_address=ADDRESS, thread_id=thread_id)
    first = FakeZcodeClient.instances[-1]
    prior = FakeZcodeClient.history[first.session_id][-1]["info"]
    assert isinstance(prior, dict)
    await tentacle.run("second", conversation_address=ADDRESS, thread_id=thread_id)
    second = FakeZcodeClient.instances[-1]
    requests = [
        params for method, params in second.calls if method == "session/messages"
    ]
    assert requests == [
        {"sessionId": first.session_id, "limit": 1},
        {"sessionId": first.session_id, "afterMessageId": prior["id"]},
    ]


async def test_model_discovery_accepts_new_names_and_derives_efforts(
    tentacle: ZcodeTentacle,
) -> None:
    config = tentacle.config
    raw = json.loads(config.desktop_config.read_text())
    raw["provider"][config.provider]["models"] = {
        "future-model": {
            "reasoning": {
                "enabled": True,
                "variants": ["high"],
                "defaultVariant": "high",
            }
        }
    }
    config.desktop_config.write_text(json.dumps(raw))
    config.claims = {"future-model": Claim("Coding")}
    await tentacle.discover_models()
    assert list(tentacle.models) == ["builtin:bigmodel:future-model"]
    assert tentacle.routes[0].claim.efforts == ("medium", "high")
    assert tentacle.routes[0].claim.ability == "Coding"
    with pytest.raises(ValueError, match="does not support effort"):
        await tentacle.run(
            "prompt", conversation_address=ADDRESS, thread_id=uuid7(), effort="low"
        )
    result = await tentacle.run(
        "prompt",
        conversation_address=ADDRESS,
        thread_id=uuid7(),
        model="builtin:bigmodel:future-model",
        effort="high",
    )
    assert result.output == "canonical prompt"


async def test_shutdown_clears_advertised_routes(tentacle: ZcodeTentacle) -> None:
    assert tentacle.routes
    await tentacle.__aexit__(None, None, None)
    assert not tentacle.routes


async def test_discovery_exposes_all_desktop_models(
    tentacle: ZcodeTentacle,
) -> None:
    config = tentacle.config
    raw = json.loads(config.desktop_config.read_text())
    raw["provider"][config.provider]["models"]["another-model"] = {}
    config.desktop_config.write_text(json.dumps(raw))
    config.claims = {"GLM-5.3": Claim("Coding")}
    await tentacle.discover_models()
    assert list(tentacle.models) == [
        "builtin:bigmodel:GLM-5.3",
        "builtin:bigmodel:another-model",
    ]
    assert tentacle.routes[0].claim.efforts == (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert tentacle.routes[1].claim.efforts == ()


@pytest.mark.parametrize("catalog_failure", ["missing", "disabled"])
async def test_discovery_does_not_advertise_unavailable_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_failure: str,
) -> None:
    class UnavailableCatalog(FakeZcodeClient):
        async def call(self, method: str, params: JsonObject) -> JsonValue:
            if method == "workspace/readState":
                return {
                    "modelCatalog": {
                        "available": []
                        if catalog_failure == "missing"
                        else [
                            {
                                "ref": {
                                    "providerId": "builtin:bigmodel",
                                    "modelId": "GLM-5.3",
                                },
                                "label": "GLM",
                                "disabledReason": "provider unavailable",
                            }
                        ]
                    }
                }
            return await super().call(method, params)

    monkeypatch.setattr(zcode_base, "ZcodeClient", UnavailableCatalog)
    agent = ZcodeTentacle("zcode", Octomate(), config=desktop_config(tmp_path))
    with pytest.raises(ValueError, match=r"did not advertise|unavailable"):
        await agent.__aenter__()
    assert not agent.routes
    assert UnavailableCatalog.instances[-1].closed


async def test_old_large_history_is_not_retrieved_for_a_new_turn(
    tentacle: ZcodeTentacle, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SizeCheckedClient(FakeZcodeClient):
        async def call(self, method: str, params: JsonObject) -> JsonValue:
            result = await super().call(method, params)
            if (
                method == "session/messages"
                and len(json.dumps(result)) > 16 * 1024 * 1024
            ):
                raise AgentRunError("history frame too large")
            return result

    thread_id = uuid7()
    await tentacle.run("first", conversation_address=ADDRESS, thread_id=thread_id)
    native_history = FakeZcodeClient.history[FakeZcodeClient.instances[-1].session_id]
    native_history.insert(
        0, history_message("old-large-answer", "x" * (17 * 1024 * 1024))
    )
    monkeypatch.setattr(zcode_base, "ZcodeClient", SizeCheckedClient)
    result = await tentacle.run(
        "second", conversation_address=ADDRESS, thread_id=thread_id
    )
    assert result.output == "canonical second"


async def test_missing_history_cursor_does_not_replay_old_turns(
    tentacle: ZcodeTentacle, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RemovedCursorClient(FakeZcodeClient):
        async def call(self, method: str, params: JsonObject) -> JsonValue:
            cursor = params.get("afterMessageId")
            if method == "session/messages" and cursor is not None:
                self.history[self.session_id] = [
                    message
                    for message in self.history[self.session_id]
                    if not isinstance(message["info"], dict)
                    or message["info"]["id"] != cursor
                ]
            return await super().call(method, params)

    thread_id = uuid7()
    await tentacle.run("first", conversation_address=ADDRESS, thread_id=thread_id)
    monkeypatch.setattr(zcode_base, "ZcodeClient", RemovedCursorClient)
    with pytest.raises(AgentRunError, match="history cursor"):
        await tentacle.run("second", conversation_address=ADDRESS, thread_id=thread_id)


async def test_subprocess_preflight_failure_releases_the_run(tmp_path: Path) -> None:
    config = desktop_config(tmp_path)
    config.command = [sys.executable, "-u", "-c", INTERACTIVE_SERVER]
    manager = FakeConversationManager()
    agent = ZcodeTentacle("zcode", Octomate(conversations=manager), config=config)
    async with asyncio.timeout(3):
        with pytest.raises(AgentRunError, match="prompt_failed"):
            await agent.run(
                "preflight-failure", conversation_address=ADDRESS, thread_id=uuid7()
            )
    assert not manager.runs
    assert not agent.live_clients
