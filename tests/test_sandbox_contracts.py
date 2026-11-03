from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    PartDeltaEvent,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.base import OctomateConfig
from octomate.config.sandbox import NativeSandboxConfig, SbxSandboxConfig
from octomate.managers.sandbox import SandboxError, SandboxManager
from octomate.nerve import (
    Impulse,
    NerveEvent,
    impulse_adapter,
)
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.sandbox import (
    ExecuteAgent,
    InvocationAccepted,
    InvocationCompleted,
    InvocationResult,
    InvocationScope,
)
from octomate.schemas.user import UserProfile

IMAGE = "octomate-sandbox@sha256:" + "a" * 64


async def test_native_default_constructs_an_idle_sandbox_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = Mock(side_effect=AssertionError("Native mode must not launch sbx"))
    monkeypatch.setattr(SbxSandbox, "cli", cli)
    app = Octomate()
    assert app.config.sandbox == NativeSandboxConfig()
    assert isinstance(app.sandboxes, SandboxManager)
    assert app.sandboxes.config is app.config.sandbox
    assert app.sandboxes.nerve is app.nerve
    assert app.sandboxes.sandboxes == {}
    await app.sandboxes.close()
    assert app.sandboxes.closed
    cli.assert_not_called()


async def test_native_manager_refuses_vm_acquisition() -> None:
    app = Octomate()
    scope = InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())
    with pytest.raises(SandboxError, match="require sbx configuration"):
        async with app.sandboxes.acquire(scope, uuid7()):
            pytest.fail("Native configuration must not allocate a VM")
    assert app.sandboxes.sandboxes == {}
    await app.sandboxes.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"image": "sandbox:latest"},
        {"mode": "docker"},
        {"cpus": 0},
        {"memory_mib": 511},
        {"idle_timeout": 0},
        {"idle_timeout": float("inf")},
        {"cpus": True},
        {"version": "0.47.0"},
        {"unknown": "setting"},
    ],
)
def test_invalid_sbx_settings_are_rejected(
    changes: dict[str, str | int | float],
) -> None:
    with pytest.raises(ValidationError):
        SbxSandboxConfig.model_validate({"image": IMAGE, **changes})


async def test_sbx_cannot_silently_serve_native_before_dispatch_is_implemented() -> (
    None
):
    app = Octomate(config=OctomateConfig(sandbox=SbxSandboxConfig(image=IMAGE)))
    assert isinstance(app.sandboxes, SandboxManager)
    assert app.sandboxes.config is app.config.sandbox
    with pytest.raises(RuntimeError, match="bootstrap verification only"):
        async with app.lifespan(app):
            pytest.fail("sbx must not reach native serving")


def test_ownership_and_mode_are_bound_to_the_registered_user() -> None:
    profile = UserProfile(user_id=uuid7())
    scope = InvocationScope.from_profile(
        profile, mode="sbx", conversation_id=uuid7(), thread_id=uuid7()
    )
    scope.require_binding(scope)
    with pytest.raises(ValueError, match="does not match"):
        scope.require_binding(scope.model_copy(update={"user_id": uuid7()}))
    with pytest.raises(ValueError, match="does not match"):
        scope.require_binding(scope.model_copy(update={"mode": "native"}))
    with pytest.raises(ValueError, match="registered user"):
        InvocationScope.from_profile(
            UserProfile(), mode="sbx", conversation_id=scope.conversation_id
        )


def test_invocation_and_terminal_history_round_trip_without_python_objects() -> None:
    request = ExecuteAgent(
        invocation_id=uuid7(),
        scope=InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7()),
        agent_id="inkling",
        model="selected-model",
        effort="low",
        prompt=["inspect", BinaryContent(data=b"\x00\xff\xfe", media_type="image/png")],
        address=ChannelAddress("im", "dm", "room", "channel-user"),
        run_name="turn",
        workspace="/home/agent/workspace",
    )
    sent = Impulse(request)
    wire = impulse_adapter.dump_json(sent)
    assert impulse_adapter.validate_json(wire) == sent
    payload = json.loads(wire)
    assert payload["event"]["kind"] == "execute"
    assert "impulse" not in payload
    assert "topic" not in payload
    result = InvocationCompleted(
        invocation_id=request.invocation_id,
        result=InvocationResult(
            run_id=uuid7(),
            output="done",
            messages=[ModelRequest(parts=[UserPromptPart(content="hello")])],
            usage=RunUsage(input_tokens=12, output_tokens=4),
            external_session_id="provider-session",
            artifact_ids=[uuid7()],
        ),
    )
    received = Impulse(result)
    assert (
        impulse_adapter.validate_json(impulse_adapter.dump_json(received)) == received
    )
    for key, value in [("kind", "shell"), ("callback", "host")]:
        payload = json.loads(wire)
        payload["event"][key] = value
        with pytest.raises(ValidationError):
            impulse_adapter.validate_json(json.dumps(payload))


def test_nerve_preserves_native_stream_delta_and_invocation_correlation() -> None:
    impulse = Impulse(
        PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hello")),
        invocation_id=uuid7(),
    )
    assert impulse_adapter.validate_json(impulse_adapter.dump_json(impulse)) == impulse


def test_impulse_adapter_accepts_signals_and_events_and_rejects_extra_fields() -> None:
    messages: list[Impulse[NerveEvent]] = [
        Impulse(InvocationAccepted(invocation_id=uuid7())),
        Impulse(PartDeltaEvent(index=0, delta=TextPartDelta("hello"))),
    ]
    for impulse in messages:
        wire = impulse_adapter.dump_json(impulse)
        assert impulse_adapter.validate_json(wire) == impulse
        payload = json.loads(wire)
        payload["topic"] = {"kind": "user", "user_id": str(uuid7())}
        with pytest.raises(ValidationError):
            impulse_adapter.validate_json(json.dumps(payload))
