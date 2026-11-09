from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate.config.base import OctomateConfig
from octomate.config.habitat import NativeHabitatConfig, SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from tests.test_habitat_contracts import IMAGE


@pytest.mark.parametrize(
    "habitat", [None, {"mode": "native"}, {"mode": "sbx", "image": IMAGE}]
)
def test_factory_builds_every_harness_without_launching_a_habitat(
    monkeypatch: pytest.MonkeyPatch, habitat: dict[str, str] | None
) -> None:
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            **({} if habitat is None else {"habitat": habitat}),
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
    monkeypatch.setattr(SbxHabitat, "cli", cli)
    with (
        patch("logfire.configure"),
        patch("logging.basicConfig"),
        patch(
            "octomate.providers.ProviderRegistry.build_model", return_value=TestModel()
        ),
    ):
        app = application.create_app()
    assert set(app.agents) == {"claude", "codex", "deepseek", "assistant", "external"}
    assert app.habitats.config == config.habitat
    for agent in app.agents.values():
        assert agent.habitats is app.habitats
    assert app.habitats.residences == {}
    cli.assert_not_called()


def test_native_is_the_default_mode() -> None:
    assert OctomateConfig().habitat == NativeHabitatConfig()


def test_sbx_mode_round_trips() -> None:
    config = OctomateConfig.model_validate(
        {"habitat": {"mode": "sbx", "image": IMAGE, "cpus": 4}}
    )
    assert config.habitat == SbxHabitatConfig(image=IMAGE, cpus=4)
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "habitat",
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
    habitat: dict[str, str] | list[dict[str, str]],
) -> None:
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate({"habitat": habitat})


async def test_each_user_gets_one_vm_shared_by_their_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HabitatManager(SbxHabitatConfig(image=IMAGE, idle_timeout=60), Nerve())
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(SbxHabitat, "stop", stop)
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    peer = owner.model_copy(update={"conversation_id": uuid7()})
    other = owner.model_copy(update={"user_id": uuid7()})
    try:
        async with (
            manager.enter(owner, uuid7()) as first,
            manager.enter(peer, uuid7()) as shared,
            manager.enter(other, uuid7()) as separate,
        ):
            assert first is shared
            assert first is not separate
            assert first.habitat.name != separate.habitat.name
            assert len(first.invocations) == 2
            assert len(separate.invocations) == 1
        await manager.stop(first)
        assert first.status.phase == "stopped"
        assert separate.status.phase == "ready"
        assert separate.needs_cleanup
        stop.assert_awaited_once()
    finally:
        await manager.close()
    assert separate.status.phase == "stopped"
    assert stop.await_count == 2
