from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate.config.base import OctomateConfig
from octomate.config.sandbox import NativeSandboxConfig, SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope
from tests.test_sandbox_contracts import IMAGE


@pytest.mark.parametrize(
    "sandbox", [None, {"mode": "native"}, {"mode": "sbx", "image": IMAGE}]
)
def test_factory_builds_every_harness_without_launching_a_sandbox(
    monkeypatch: pytest.MonkeyPatch, sandbox: dict[str, str] | None
) -> None:
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            **({} if sandbox is None else {"sandbox": sandbox}),
            "tentacles": {
                "claude": {"type": "claude"},
                "codex": {"type": "codex"},
                "deepseek": {"type": "deepseek"},
                "assistant": {
                    "type": "inkling",
                    "models": [{"name": "openai:gpt-4.1-mini"}],
                },
                "external": {
                    "type": "acp_agent",
                    "provider": {"command": "unused-agent"},
                },
            },
        }
    )
    monkeypatch.setattr(application, "config", config)
    cli = Mock(side_effect=AssertionError("Binding must not launch a VM"))
    monkeypatch.setattr(SbxSandbox, "cli", cli)
    with (
        patch("logfire.configure"),
        patch("logging.basicConfig"),
        patch(
            "octomate.providers.ProviderRegistry.build_model", return_value=TestModel()
        ),
    ):
        app = application.create_app()
    assert set(app.agents) == {"claude", "codex", "deepseek", "assistant", "external"}
    assert app.sandboxes.config == config.sandbox
    for agent in app.agents.values():
        assert agent.sandboxes is app.sandboxes
    assert app.sandboxes.sandboxes == {}
    cli.assert_not_called()


def test_native_is_the_default_mode() -> None:
    assert OctomateConfig().sandbox == NativeSandboxConfig()


def test_sbx_mode_round_trips() -> None:
    config = OctomateConfig.model_validate(
        {"sandbox": {"mode": "sbx", "image": IMAGE, "cpus": 4}}
    )
    assert config.sandbox == SbxSandboxConfig(image=IMAGE, cpus=4)
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "sandbox",
    [
        {"image": IMAGE},
        {"mode": "srt"},
        {"mode": "cloud", "image": IMAGE},
        {"mode": "sbx"},
        {"mode": "native", "image": IMAGE},
        [{"mode": "sbx", "image": IMAGE}],
    ],
)
def test_invalid_modes_are_rejected(
    sandbox: dict[str, str] | list[dict[str, str]],
) -> None:
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate({"sandbox": sandbox})


async def test_each_user_gets_one_vm_shared_by_their_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SandboxManager(SbxSandboxConfig(image=IMAGE, idle_timeout=60), Nerve())
    monkeypatch.setattr(SbxSandbox, "prepare", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(SbxSandbox, "stop", stop)
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    peer = owner.model_copy(update={"conversation_id": uuid7()})
    other = owner.model_copy(update={"user_id": uuid7()})
    try:
        async with (
            manager.acquire(owner, uuid7()) as first,
            manager.acquire(peer, uuid7()) as shared,
            manager.acquire(other, uuid7()) as separate,
        ):
            assert first is shared
            assert first is not separate
            assert first.sandbox.name != separate.sandbox.name
            assert len(first.invocations) == 2
            assert len(separate.invocations) == 1
        await manager.stop(first)
        assert first.snapshot.phase == "stopped"
        assert separate.snapshot.phase == "ready"
        assert separate.needs_cleanup
        stop.assert_awaited_once()
    finally:
        await manager.close()
    assert separate.snapshot.phase == "stopped"
    assert stop.await_count == 2
