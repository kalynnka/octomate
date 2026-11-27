from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import HttpUrl, ValidationError
from pydantic_ai.messages import (
    PartDeltaEvent,
    TextPartDelta,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.base import OctomateConfig
from octomate.config.habitat import (
    NativeHabitatConfig,
    SbxHabitatConfig,
)
from octomate.events import HabitatProgressEvent
from octomate.habitat import NativeHabitat
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatError, HabitatManager
from octomate.nerve import (
    Impulse,
    impulse_adapter,
)
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.user import UserProfile

IMAGE = "octomate-sandbox@sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("http://192.0.2.10:8123", "http://192.0.2.10:8123"),
        ("https://core.example:8443", "https://core.example:8443"),
        ("http://0.0.0.0:8123", "http://127.0.0.1:8123"),
        ("http://[::]:8123", "http://[::1]:8123"),
    ],
)
def test_native_habitat_translates_its_core_address(
    address: str, expected: str
) -> None:
    configured = NativeHabitatConfig(core_address=HttpUrl(address))
    habitat = NativeHabitat(config=configured)
    assert habitat.config is configured
    assert habitat.core_url == expected


async def test_native_default_constructs_an_idle_habitat_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = Mock(side_effect=AssertionError("Native mode must not launch sbx"))
    monkeypatch.setattr(SbxHabitat, "cli", cli)
    app = Octomate()
    assert app.config.habitats["native"] == NativeHabitatConfig()
    assert isinstance(app.habitats["native"], HabitatManager)
    assert app.habitats["native"].config is app.config.habitats["native"]
    assert app.habitats["native"].nerve is app.nerve
    assert app.habitats["native"].residences == {}
    await app.habitats["native"].close()
    assert app.habitats["native"].closed
    cli.assert_not_called()


async def test_native_manager_refuses_vm_acquisition() -> None:
    app = Octomate()
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    with pytest.raises(HabitatError, match="provider is 'native'"):
        async with app.habitats["native"].enter(scope, uuid7()):
            pytest.fail("Native configuration must not allocate a VM")
    assert app.habitats["native"].residences == {}
    await app.habitats["native"].close()


async def test_shutdown_drains_all_named_pools_after_one_failure(
    monkeypatch: pytest.MonkeyPatch, in_memory_engine: AsyncEngine
) -> None:
    app = Octomate(
        config=OctomateConfig(
            habitats={"first": NativeHabitatConfig(), "second": NativeHabitatConfig()}
        )
    )
    failed = AsyncMock(side_effect=HabitatError("stop failed"))
    stopped = AsyncMock()
    monkeypatch.setattr(app.habitats["first"], "close", failed)
    monkeypatch.setattr(app.habitats["second"], "close", stopped)
    with pytest.RaisesGroup(pytest.RaisesExc(HabitatError, match="stop failed")):
        async with app.lifespan(app):
            pass
    failed.assert_awaited_once()
    stopped.assert_awaited_once()


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
        {"denied_domains": ["https://example.com/path"]},
        {"denied_domains": ["example.com:65536"]},
        {"version": "0.47.0"},
        {"unknown": "setting"},
        {"core_address": "ftp://core.example:8000"},
        {"core_address": "not a URL"},
    ],
)
def test_invalid_sbx_settings_are_rejected(
    changes: dict[str, str | int | float | list[str]],
) -> None:
    with pytest.raises(ValidationError):
        SbxHabitatConfig.model_validate({"image": IMAGE, **changes})


def test_sbx_internet_access_is_enabled_by_default() -> None:
    assert SbxHabitatConfig(image=IMAGE).internet_access


def test_sbx_idle_timeout_defaults_to_five_minutes() -> None:
    assert SbxHabitatConfig(image=IMAGE).idle_timeout == 300


async def test_sbx_application_lifespan_starts_without_allocating_a_vm(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = Mock(side_effect=AssertionError("Startup must not launch sbx"))
    monkeypatch.setattr(SbxHabitat, "cli", cli)
    app = Octomate(
        config=OctomateConfig(habitats={"native": SbxHabitatConfig(image=IMAGE)})
    )
    async with app.lifespan(app) as state:
        assert state["octomate"] is app
        assert app.habitats["native"].config is app.config.habitats["native"]
        assert app.habitats["native"].residences == {}
    cli.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
async def test_application_shutdown_releases_habitats(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    prepare, stop, release = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(SbxHabitat, "prepare", prepare)
    monkeypatch.setattr(SbxHabitat, "stop", stop)
    app = Octomate(
        config=OctomateConfig(habitats={"native": SbxHabitatConfig(image=IMAGE)})
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    with (
        pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="probe"))
        if failed
        else nullcontext()
    ):
        async with app.lifespan(app):
            async with app.habitats["native"].enter(scope, uuid7()) as residence:
                residence.habitat.resources.push_async_callback(release)
            assert residence.idle is not None
            if failed:
                raise RuntimeError("probe")
    release.assert_awaited_once()
    stop.assert_awaited_once()
    assert app.habitats["native"].closed
    assert residence.idle is None
    assert residence.status.phase == "stopped"


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
            HabitatProgressEvent(
                user_id=uuid7(),
                habitat="local",
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
