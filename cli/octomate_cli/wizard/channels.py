from __future__ import annotations

from functools import partial

from rich.console import Console

from octomate_cli.wizard.base import TentacleSetup


def configure_channel(name: str, console: Console) -> str:
    if name == "trunkline":
        console.print("Trunkline's API is enabled; its frontend is built separately.")
    else:
        console.print(
            f"{CHANNELS[name].label}: fill credentials in config/channels.yaml, then enable channels.{name}.enabled."
        )
    return name


CHANNELS: dict[str, TentacleSetup[str]] = {
    "trunkline": TentacleSetup(
        label="Trunkline [Web]",
        capabilities=("Channel",),
        configure=partial(configure_channel, "trunkline"),
        checked=True,
    ),
    "slack": TentacleSetup(
        label="Slack",
        capabilities=("Channel", "MCP"),
        configure=partial(configure_channel, "slack"),
    ),
    "lark": TentacleSetup(
        label="Lark",
        capabilities=("Channel",),
        configure=partial(configure_channel, "lark"),
    ),
    "discord": TentacleSetup(
        label="Discord",
        capabilities=("Channel",),
        configure=partial(configure_channel, "discord"),
    ),
}
