from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.agents import InklingConfig
from octomate.config.base import OctomateConfig
from octomate.config.models import ModelConfig
from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope
from octomate.tentacles.inkling import InklingTentacle
from tests.test_sandbox_contracts import IMAGE


@pytest.mark.parametrize("sandbox", [None, "local", "batch"])
def test_factory_assigns_each_harness_without_launching_a_sandbox(
    monkeypatch: pytest.MonkeyPatch, sandbox: str | None
) -> None:
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            "sandbox": [
                {"id": "local", "mode": "sbx", "image": IMAGE},
                {"id": "batch", "mode": "sbx", "image": IMAGE, "cpus": 4},
            ],
            "tentacles": {
                "claude": {"type": "claude", "sandbox": sandbox},
                "codex": {"type": "codex", "sandbox": sandbox},
                "deepseek": {"type": "deepseek", "sandbox": sandbox},
                "assistant": {
                    "type": "inkling",
                    "sandbox": sandbox,
                    "models": [{"name": "openai:gpt-4.1-mini"}],
                },
                "external": {
                    "type": "acp_agent",
                    "sandbox": sandbox,
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
    for agent in app.agents.values():
        assert agent.sandboxes is app.sandboxes
        assert agent.sandbox == sandbox
    assert app.sandboxes.sandboxes == {}
    cli.assert_not_called()


@pytest.mark.parametrize("sandbox", [None, "batch"])
def test_connect_preserves_the_tentacles_constructor_selection(
    sandbox: str | None,
) -> None:
    app = Octomate(
        config=OctomateConfig(
            sandbox=[
                SbxSandboxConfig(id=name, image=IMAGE) for name in ("local", "batch")
            ],
            tentacles={
                "assistant": InklingConfig(
                    sandbox="local", models=[ModelConfig(name="openai:gpt-4.1-mini")]
                )
            },
        )
    )
    sandboxes = SandboxManager(app.config.sandbox, app.nerve)
    agent = InklingTentacle(
        "assistant",
        app,
        auth=app.auth,
        sandboxes=sandboxes,
        agent=Agent(TestModel()),
        sandbox=sandbox,
    )
    assert agent.sandboxes is sandboxes
    assert agent.sandboxes is not app.sandboxes
    assert agent.sandbox == sandbox
    app.connect(agent)
    assert agent.sandboxes is sandboxes
    assert agent.sandbox == sandbox


def test_sandbox_definitions_and_agent_assignments_round_trip() -> None:
    config = OctomateConfig.model_validate(
        {
            "sandbox": [{"id": "local", "image": IMAGE}],
            "tentacles": {
                "claude": {"type": "claude", "sandbox": "local"},
                "codex": {"type": "codex"},
            },
        }
    )
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


async def test_unused_deployments_do_not_block_native_serving(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate(
        config=OctomateConfig(sandbox=[SbxSandboxConfig(id="local", image=IMAGE)])
    )
    agent = app.connect(
        InklingTentacle(
            "bare",
            app,
            auth=app.auth,
            sandboxes=app.sandboxes,
            agent=Agent(TestModel()),
        )
    )
    async with app.lifespan(app):
        assert agent.sandbox is None
        assert app.sandboxes.sandboxes == {}


def test_duplicate_sandbox_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="IDs must be unique"):
        OctomateConfig(
            sandbox=[
                SbxSandboxConfig(id="local", image=IMAGE),
                SbxSandboxConfig(id="local", image=IMAGE, cpus=4),
            ]
        )


@pytest.mark.parametrize("sandbox", ["missing", ""])
def test_unknown_or_empty_agent_assignment_is_rejected(sandbox: str) -> None:
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate(
            {"tentacles": {"claude": {"type": "claude", "sandbox": sandbox}}}
        )


@pytest.mark.parametrize("mode", ["native", "cloud"])
def test_only_sbx_definitions_are_supported(mode: str) -> None:
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate(
            {"sandbox": [{"id": "local", "image": IMAGE, "mode": mode}]}
        )


async def test_user_vms_are_shared_within_a_deployment_and_independent_between_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = [
        SbxSandboxConfig(id=name, image=IMAGE, idle_timeout=60)
        for name in ("local", "batch")
    ]
    manager = SandboxManager(configs, Nerve())
    monkeypatch.setattr(SbxSandbox, "prepare", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(SbxSandbox, "stop", stop)
    local = InvocationScope(user_id=uuid7(), sandbox="local", conversation_id=uuid7())
    peer = local.model_copy(update={"conversation_id": uuid7()})
    batch = local.model_copy(update={"sandbox": "batch"})
    try:
        async with (
            manager.acquire(local, uuid7()) as first,
            manager.acquire(peer, uuid7()) as shared,
            manager.acquire(batch, uuid7()) as separate,
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
