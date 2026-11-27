from __future__ import annotations

import importlib
from dataclasses import fields
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.base import OctomateConfig
from octomate.config.habitat import (
    NativeHabitatConfig,
    SbxHabitatConfig,
    SrtHabitatConfig,
)
from octomate.habitat.native import NativeHabitat
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
            **({} if habitat is None else {"habitats": {"native": habitat}}),
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
    assert app.habitats["native"].config == config.habitats["native"]
    for agent in app.agents.values():
        assert agent.habitats is app.habitats["native"]
    assert app.habitats["native"].residences == {}
    cli.assert_not_called()


def test_native_is_the_default_mode() -> None:
    assert OctomateConfig().habitats["native"] == NativeHabitatConfig()


def test_backend_types_are_class_metadata_and_native_instances_have_distinct_ids() -> (
    None
):
    for cls, kind in (
        (NativeHabitat, "native"),
        (SbxHabitat, "sbx"),
        (SrtHabitat, "srt"),
    ):
        assert cls.type == kind
        assert "type" not in {field.name for field in fields(cls)}
    first, second = NativeHabitat(), NativeHabitat()
    identifier = first.id
    assert first.id == identifier
    assert first.id != second.id


def test_unknown_agent_habitat_is_rejected() -> None:
    with pytest.raises(ValidationError, match="selects unknown habitat 'missing'"):
        OctomateConfig.model_validate(
            {"tentacles": {"codex": {"type": "codex", "habitat": "missing"}}}
        )


def test_service_wide_habitat_requires_explicit_migration() -> None:
    with pytest.raises(ValidationError, match="Replace service-wide habitat"):
        OctomateConfig.model_validate({"habitat": {"mode": "native"}})


@pytest.mark.parametrize("name", ["", "../shared", "mixed name", "x" * 49])
def test_habitat_names_are_safe_runtime_identifiers(name: str) -> None:
    with pytest.raises(ValidationError):
        OctomateConfig.model_validate({"habitats": {name: {"mode": "native"}}})


async def test_agents_share_selected_pool_per_user_and_shutdown_all_pools(
    monkeypatch: pytest.MonkeyPatch, in_memory_engine: AsyncEngine
) -> None:
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            "habitats": {
                "local-srt": {"mode": "srt"},
                "shared-sbx": {"mode": "sbx", "image": IMAGE},
                "other-sbx": {"mode": "sbx", "image": IMAGE},
            },
            "tentacles": {
                "claude": {"type": "claude", "habitat": "local-srt"},
                "codex": {"type": "codex", "habitat": "shared-sbx"},
                "deepseek": {"type": "deepseek", "habitat": "shared-sbx"},
            },
        }
    )
    monkeypatch.setattr(application, "config", config)
    sbx_start, sbx_stop = AsyncMock(), AsyncMock()
    srt_start, srt_stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(SbxHabitat, "prepare", sbx_start)
    monkeypatch.setattr(SbxHabitat, "stop", sbx_stop)
    monkeypatch.setattr(SrtHabitat, "prepare", srt_start)
    monkeypatch.setattr(SrtHabitat, "stop", srt_stop)
    with patch("logfire.configure"), patch("logging.basicConfig"):
        app = application.create_app()
    claude, codex, deepseek = (
        app.agents[name] for name in ("claude", "codex", "deepseek")
    )
    assert codex.habitats is deepseek.habitats is app.habitats["shared-sbx"]
    assert codex.habitats.name == "shared-sbx"
    assert claude.habitats is app.habitats["local-srt"]
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sibling = owner.model_copy(update={"conversation_id": uuid7()})
    other_user = owner.model_copy(update={"user_id": uuid7()})
    async with app.lifespan(app):
        async with (
            codex.habitats.enter(owner, uuid7()) as first,
            deepseek.habitats.enter(sibling, uuid7()) as shared,
            deepseek.habitats.enter(other_user, uuid7()) as separate_user,
            claude.habitats.enter(owner, uuid7()) as srt,
            app.habitats["other-sbx"].enter(owner, uuid7()) as separate_pool,
        ):
            assert first is shared
            assert isinstance(first.habitat, SbxHabitat)
            assert first.habitat.name == "shared-sbx"
            assert len(first.invocations) == 2
            assert first is not separate_user
            assert first is not separate_pool
            assert isinstance(srt.habitat, SrtHabitat)
            assert (
                len({r.habitat.id for r in (first, separate_user, separate_pool, srt)})
                == 4
            )
            assert sbx_start.await_count == 3
            srt_start.assert_awaited_once()
        assert first.idle is not None
    assert all(manager.closed for manager in app.habitats.values())
    assert sbx_stop.await_count == 3
    srt_stop.assert_awaited_once()
    assert all(
        not residence.needs_cleanup
        for manager in app.habitats.values()
        for residence in manager.residences.values()
    )


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek"])
def test_srt_accepts_supported_agents(agent: str) -> None:
    config = OctomateConfig.model_validate(
        {
            "habitats": {"native": {"mode": "srt"}},
            "tentacles": {agent: {"type": agent}},
        }
    )
    assert config.tentacles[agent].enabled


@pytest.mark.parametrize("agent", ["acp_agent", "inkling"])
@pytest.mark.parametrize("enabled", [False, True])
def test_srt_rejects_enabled_unsupported_agents(agent: str, enabled: bool) -> None:
    config = {
        "habitats": {"native": {"mode": "srt"}},
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
        with pytest.raises(
            ValidationError,
            match="SRT habitats support only Claude, Codex and DeepSeek",
        ):
            OctomateConfig.model_validate(config)
    else:
        assert not OctomateConfig.model_validate(config).tentacles[agent].enabled


@pytest.mark.parametrize("field", ["os_sandbox", "builtin_sandbox"])
def test_habitat_rejects_removed_sandbox_switch(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OctomateConfig.model_validate(
            {"habitats": {"native": {"mode": "srt", field: False}}}
        )


def test_sbx_mode_round_trips() -> None:
    config = OctomateConfig.model_validate(
        {"habitats": {"native": {"mode": "sbx", "image": IMAGE, "cpus": 4}}}
    )
    assert config.habitats["native"] == SbxHabitatConfig(image=IMAGE, cpus=4)
    assert OctomateConfig.model_validate_json(config.model_dump_json()) == config


def test_srt_mode_round_trips() -> None:
    config = OctomateConfig.model_validate({"habitats": {"native": {"mode": "srt"}}})
    assert config.habitats["native"] == SrtHabitatConfig()
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
        OctomateConfig.model_validate({"habitats": {"native": habitat}})


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
            assert first.habitat.id != separate.habitat.id
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
