"""Disposable owner storage with real SRT confinement and no model credentials."""

import os
import platform
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from uuid_utils.compat import uuid7

from octomate.config.habitat import SrtHabitatConfig
from octomate.habitat.srt import SrtHabitat


@pytest.fixture
async def habitat(tmp_path: Path) -> AsyncGenerator[SrtHabitat]:
    if platform.system() != "Darwin":
        pytest.skip("SRT habitat is currently qualified for macOS only")
    executable = os.environ.get("SRT_TEST_EXECUTABLE")
    if not executable:
        pytest.fail("Set SRT_TEST_EXECUTABLE to the pinned SRT executable")
    habitat = SrtHabitat(
        config=SrtHabitatConfig(executable=executable), user_id=uuid7(), root=tmp_path
    )
    try:
        await habitat.prepare()
        yield habitat
    finally:
        await habitat.stop()
    assert not habitat.processes
