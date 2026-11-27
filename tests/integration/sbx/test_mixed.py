"""Named SRT and sbx habitats coexist, share by owner, and expire independently."""

import asyncio
import importlib
import os
import platform
from contextlib import AsyncExitStack
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config import OctomateConfig
from octomate.events import HabitatProgressEvent
from octomate.habitat.sbx import SbxHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.nerve import HabitatTopic
from octomate.schemas.habitat import InvocationScope
from tests.integration.sbx import remove_sandbox


@pytest.mark.timeout(600)
async def test_named_habitats_share_isolate_and_expire(
    pytestconfig: pytest.Config,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
) -> None:
    if not pytestconfig.getoption("--srt") or platform.system() != "Darwin":
        pytest.skip("Mixed acceptance requires --sbx --srt on macOS")
    image = os.environ.get("SBX_TEST_IMAGE")
    executable = os.environ.get("SRT_TEST_EXECUTABLE")
    if not image or not executable:
        pytest.fail("Set SBX_TEST_IMAGE and SRT_TEST_EXECUTABLE for mixed acceptance")
    application = importlib.import_module("octomate.app")
    config = OctomateConfig.model_validate(
        {
            "habitats": {
                "local-srt": {
                    "provider": "srt",
                    "executable": executable,
                    "idle_timeout": 0.1,
                },
                "shared-sbx": {"provider": "sbx", "image": image, "idle_timeout": 0.1},
            },
            "tentacles": {
                "claude": {"type": "claude", "habitat": "local-srt"},
                "codex": {"type": "codex", "habitat": "shared-sbx"},
                "deepseek": {"type": "deepseek", "habitat": "shared-sbx"},
            },
        }
    )
    monkeypatch.setattr(application, "config", config)
    with patch("logfire.configure"), patch("logging.basicConfig"):
        app = application.create_app()
    claude, codex, dsh = (app.agents[name] for name in ("claude", "codex", "deepseek"))
    assert codex.habitats is dsh.habitats is app.habitats["shared-sbx"]
    assert claude.habitats is app.habitats["local-srt"]
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sibling = owner.model_copy(update={"conversation_id": uuid7()})
    other = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    for scope in (owner, other):
        sandbox = codex.habitats.residence(scope).habitat
        assert isinstance(sandbox, SbxHabitat)
        sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    for manager in app.habitats.values():
        sbx_cleanup.push_async_callback(manager.close)

    async with (
        app.nerve.subscribe(HabitatTopic(owner.user_id)) as owner_events,
        app.nerve.subscribe(HabitatTopic(other.user_id)) as other_events,
    ):
        async with (
            codex.habitats.enter(owner, uuid7()) as first,
            dsh.habitats.enter(sibling, uuid7()) as shared,
            dsh.habitats.enter(other, uuid7()) as separate,
            claude.habitats.enter(owner, uuid7()) as srt,
        ):
            assert first is shared
            assert len(first.invocations) == 2
            assert isinstance(srt.habitat, SrtHabitat)
            assert len({first.habitat.id, separate.habitat.id, srt.habitat.id}) == 3
            await asyncio.sleep(0.2)
            assert all(
                state.status.phase == "ready" for state in (first, separate, srt)
            )
            assert all(state.idle is None for state in (first, separate, srt))
            linux, macos = await asyncio.gather(
                first.habitat.run(["uname", "-s"], workdir=str(first.habitat.root)),
                srt.habitat.run(["uname", "-s"], workdir=str(srt.habitat.root)),
            )
            assert linux.exit_code == macos.exit_code == 0, (linux.stderr, macos.stderr)
            assert linux.stdout.strip() == "Linux"
            assert macos.stdout.strip() == "Darwin"

        async with asyncio.timeout(60):
            for events, remaining in (
                (owner_events, {first.habitat.id, srt.habitat.id}),
                (other_events, {separate.habitat.id}),
            ):
                while remaining:
                    event = (await events.receive()).event
                    assert isinstance(event, HabitatProgressEvent), event
                    if event.phase == "stopped":
                        remaining.discard(event.habitat)
    for state in (first, separate, srt):
        assert state.stopping is not None
        await state.stopping
        assert not state.needs_cleanup
        assert not state.invocations
    assert not srt.habitat.processes
