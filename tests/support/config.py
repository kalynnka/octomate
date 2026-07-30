"""The config a unit test builds, isolated from the machine it runs on."""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from octomate.config import OctomateConfig


class IsolatedTestConfig(OctomateConfig):
    """`OctomateConfig` reading octomate.test.yaml, never the developer's own.

    Identical validation — it is the same class — with its file sources pointed at
    a committed, empty deployment. `octomate.yaml` and `.env` are gitignored, so a
    test that merged them would assert against whatever this machine happens to
    hold: green here, red on a bare checkout, and broken by adding a user locally.

    Use it wherever a test builds a config from a literal payload. The live replay
    suites under tests/trigger want the real files and keep using `OctomateConfig`.
    """

    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE__",
        env_nested_delimiter="__",
        yaml_file=("octomate.test.yaml",),
        yaml_config_section="octomate",
        # Spelled out because `model_config` merges down the hierarchy: omitting it
        # would inherit `.env` rather than drop it, and half a channel's secrets
        # arriving from the environment fails validation on the half that did not.
        env_file=None,
        nested_model_default_partial_update=True,
        hide_input_in_errors=True,
        extra="ignore",
    )
