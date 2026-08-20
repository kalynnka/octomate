"""The config home a unit test runs against, isolated from the machine it runs on."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from pydantic_settings import BaseSettings

from octomate.config import DatabaseSettings, OctomateConfig

# `tests/config/`, resolved from this file so the suite works from any working
# directory. Declares nothing: a test sees the packaged defaults plus its own payload.
ISOLATED_HOME: Final[Path] = Path(__file__).parent.parent / "config"

SETTINGS_CLASSES: Final[tuple[type[BaseSettings], ...]] = (
    OctomateConfig,
    DatabaseSettings,
)


@contextmanager
def without_dotenv() -> Generator[None]:
    """Drop the `.env` source for the duration.

    `OCTOMATE_HOME` moves the yaml and clearing the environment covers exported
    variables, but neither reaches the dotenv file source: `.env` is read from the
    working directory, which for the suite is the developer's own checkout. Half a
    channel's secrets arriving from it fails the config on the half it did not
    supply — which is not theoretical, an editor that loads `.env` for the test run
    (VS Code does by default) produces exactly that.

    A test cannot opt out on behalf of the CLI, which builds its own settings several
    frames down, so the classes themselves move.
    """
    previous = [settings.model_config.get("env_file") for settings in SETTINGS_CLASSES]
    for settings in SETTINGS_CLASSES:
        settings.model_config["env_file"] = None
    try:
        yield
    finally:
        for settings, env_file in zip(SETTINGS_CLASSES, previous, strict=True):
            settings.model_config["env_file"] = env_file
