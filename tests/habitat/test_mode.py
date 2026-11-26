from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from uuid_utils.compat import uuid7

from octomate.config.base import OctomateConfig
from octomate.config.habitat import (
    NativeHabitatConfig,
    SbxHabitatConfig,
    SrtHabitatConfig,
)
from octomate.habitat.sbx import SbxHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from tests.habitat.test_contracts import IMAGE
from tests.support.managers import a_workspace_manager


@pytest.mark.parametrize(
    "habitat",
    [None, {"mode": "native"}, {"mode": "sbx", "image": IMAGE}],
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


def test_srt_accepts_claude() -> None:
    config = OctomateConfig.model_validate(
        {
            "habitat": {"mode": "srt"},
            "tentacles": {"claude": {"type": "claude"}},
        }
    )
    assert config.tentacles["claude"].enabled


@pytest.mark.parametrize("agent", ["codex", "deepseek", "acp_agent", "inkling"])
@pytest.mark.parametrize("enabled", [False, True])
def test_srt_rejects_enabled_unsupported_agents(agent: str, enabled: bool) -> None:
    config = {
        "habitat": {"mode": "srt"},
        "tentacles": {
            agent: {
                "type": agent,
                "enabled": enabled,
                **({"provider": {"command": "unused"}} if agent == "acp_agent" else {}),
                **({"models": [{"name": "test"}]} if agent == "inkling" else {}),
            }
        },
    }
    if enabled:
        with pytest.raises(ValidationError, match="SRT habitats support only Claude"):
            OctomateConfig.model_validate(config)
    else:
        assert not OctomateConfig.model_validate(config).tentacles[agent].enabled


@pytest.mark.parametrize("field", ["os_sandbox", "builtin_sandbox"])
def test_habitat_rejects_removed_sandbox_switch(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OctomateConfig.model_validate({"habitat": {"mode": "srt", field: False}})


def test_sbx_mode_round_trips() -> None:
    config = OctomateConfig.model_validate(
        {"habitat": {"mode": "sbx", "image": IMAGE, "cpus": 4}}
    )
    assert config.habitat == SbxHabitatConfig(image=IMAGE, cpus=4)
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


def test_srt_mode_round_trips() -> None:
    config = OctomateConfig.model_validate({"habitat": {"mode": "srt"}})
    assert config.habitat == SrtHabitatConfig()
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "habitat",
    [
        {"image": IMAGE},
        {"mode": "srt", "version": "unsupported"},
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


@pytest.mark.parametrize("provider", [SbxHabitat, SrtHabitat])
async def test_each_user_gets_one_habitat_shared_by_their_invocations(
    monkeypatch: pytest.MonkeyPatch,
    provider: type[SbxHabitat] | type[SrtHabitat],
) -> None:
    manager = HabitatManager(
        SbxHabitatConfig(image=IMAGE, idle_timeout=60)
        if provider is SbxHabitat
        else SrtHabitatConfig(),
        Nerve(),
        a_workspace_manager(),
    )
    monkeypatch.setattr(provider, "prepare", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(provider, "stop", stop)
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
