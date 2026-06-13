"""Fixtures for the @trigger live-replay layer.

Trigger tests replay the canonical scenario scripts through a REAL channel
tentacle into a real chat, so a human can inspect the rendering in the IM
client. They need two things from the gitignored octomate.yaml: the channel's
credentials (the regular `octomate.channels` section) and a `trigger:` section
naming the target chat per channel:

    trigger:
      slack: {chat_id: D0123, user_id: U0123}
      lark: {chat_type: group, chat_id: oc_xxx, user_id: ou_xxx}

`OctomateConfig` reads only the `octomate:` yaml section, so the sibling
`trigger:` section is invisible to the application. Tests skip cleanly when
either piece is missing.

Trigger tests never run as part of a full-suite run: a collection hook below
skips them unless the invocation explicitly targets tests/trigger (a file,
directory, or single test id — which is what an IDE test explorer sends when
you click one) or selects the marker. So both of these fire live:

    pytest tests/trigger/test_slack.py
    pytest -m trigger
"""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from octomate.config import OctomateConfig

# Real images for the showcase live in tests/src/images (see tests/src/README);
# the generated 1x1 transparent PNG below is only the fallback when none is there.
SCENARIO_SRC = Path(__file__).parent.parent / "src" / "images"
IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp")
SCENARIO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def run_banner(note: str) -> str:
    """The visible per-run notice each channel posts before its replays."""
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"🧪 Octomate trigger run {started} — {note}"


class TriggerTarget(BaseModel):
    chat_type: Literal["private", "group"] = "private"
    chat_id: str
    user_id: str
    thread_id: str = ""


class TriggerTargets(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file="octomate.yaml",
        yaml_config_section="trigger",
        extra="ignore",
    )

    slack: TriggerTarget | None = None
    lark: TriggerTarget | None = None
    napcat: TriggerTarget | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, YamlConfigSettingsSource(settings_cls))


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip trigger tests unless they were explicitly targeted.

    This keeps them visible to IDE test explorers (a plain `pytest tests`
    discovery collects them) while guaranteeing a full-suite run never fires
    live IM messages. Naming anything under tests/trigger on the command line
    — or selecting the marker — counts as explicit intent.
    """
    if config.option.markexpr == "trigger":
        return
    if any(
        "tests/trigger" in str(arg).replace("\\", "/")
        for arg in config.invocation_params.args
    ):
        return
    skip = pytest.mark.skip(
        reason="live trigger test; run its file/id directly or use -m trigger"
    )
    for item in items:
        if item.get_closest_marker("trigger") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_config() -> OctomateConfig:
    return OctomateConfig()


@pytest.fixture(scope="session")
def trigger_targets() -> TriggerTargets:
    try:
        return TriggerTargets()
    except KeyError:
        # No `trigger:` section in octomate.yaml — every trigger test skips.
        # model_construct bypasses the settings sources (which would re-read
        # the yaml and raise again).
        return TriggerTargets.model_construct(slack=None, lark=None, napcat=None)


@pytest.fixture
def scenario_image(tmp_path: Path) -> str:
    for pattern in IMAGE_PATTERNS:
        for image in sorted(SCENARIO_SRC.glob(pattern)):
            return str(image)
    fallback = tmp_path / "octomate-scenario.png"
    fallback.write_bytes(SCENARIO_PNG)
    return str(fallback)
