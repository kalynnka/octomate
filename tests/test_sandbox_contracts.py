from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import (
    PartDeltaEvent,
    TextPartDelta,
)
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.agents import InklingConfig
from octomate.config.base import OctomateConfig
from octomate.config.models import ModelConfig
from octomate.config.sandbox import NativeSandboxConfig, SbxSandboxConfig
from octomate.managers.sandbox import SandboxError, SandboxManager
from octomate.nerve import (
    Impulse,
    impulse_adapter,
)
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import (
    InvocationScope,
    SandboxProgress,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.inkling import InklingTentacle

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
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    with pytest.raises(SandboxError, match="mode is 'native'"):
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
        {"internet_access": "yes"},
        {"version": "0.47.0"},
        {"unknown": "setting"},
    ],
)
def test_invalid_sbx_settings_are_rejected(
    changes: dict[str, str | int | float],
) -> None:
    with pytest.raises(ValidationError):
        SbxSandboxConfig.model_validate({"image": IMAGE, **changes})


def test_sbx_internet_access_is_enabled_by_default() -> None:
    assert SbxSandboxConfig(image=IMAGE).internet_access


async def test_sbx_cannot_silently_serve_native_before_dispatch_is_implemented() -> (
    None
):
    app = Octomate(
        config=OctomateConfig(
            sandbox=SbxSandboxConfig(image=IMAGE),
            tentacles={
                "inkling": InklingConfig(
                    models=[ModelConfig(name="openai:gpt-4.1-mini")]
                )
            },
        )
    )
    app.connect(
        InklingTentacle(
            "inkling",
            app,
            auth=app.auth,
            sandboxes=app.sandboxes,
            agent=Agent(TestModel()),
        )
    )
    assert app.sandboxes.config is app.config.sandbox
    with pytest.raises(RuntimeError, match="not yet wired into React"):
        async with app.lifespan(app):
            pytest.fail("sbx must not reach native serving")


def test_ownership_is_bound_to_the_registered_user() -> None:
    profile = UserProfile(user_id=uuid7())
    scope = InvocationScope.from_profile(
        profile, conversation_id=uuid7(), thread_id=uuid7()
    )
    scope.require_binding(scope)
    with pytest.raises(ValueError, match="does not match"):
        scope.require_binding(scope.model_copy(update={"user_id": uuid7()}))
    with pytest.raises(ValueError, match="does not match"):
        scope.require_binding(scope.model_copy(update={"conversation_id": uuid7()}))
    with pytest.raises(ValueError, match="registered user"):
        InvocationScope.from_profile(
            UserProfile(), conversation_id=scope.conversation_id
        )


def test_nerve_preserves_native_stream_delta() -> None:
    impulse = Impulse(
        PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hello")),
    )
    assert impulse_adapter.validate_json(impulse_adapter.dump_json(impulse)) == impulse


def test_impulse_adapter_accepts_lifecycle_and_run_events_and_rejects_extra_fields() -> (
    None
):
    messages: list[Impulse] = [
        Impulse(
            SandboxProgress(
                user_id=uuid7(),
                sandbox="local",
                sequence=1,
                phase="ready",
                invocation_id=uuid7(),
            )
        ),
        Impulse(PartDeltaEvent(index=0, delta=TextPartDelta("hello"))),
    ]
    for impulse in messages:
        wire = impulse_adapter.dump_json(impulse)
        assert impulse_adapter.validate_json(wire) == impulse
        payload = json.loads(wire)
        payload["topic"] = {"kind": "user", "user_id": str(uuid7())}
        with pytest.raises(ValidationError):
            impulse_adapter.validate_json(json.dumps(payload))
