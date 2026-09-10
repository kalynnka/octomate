from __future__ import annotations

from functools import partial

from rich.console import Console

from octomate_cli.wizard.base import TentacleSetup, brand_color, select_tentacles


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
        label="Trunkline (API; frontend built separately)",
        configure=partial(configure_channel, "trunkline"),
        checked=True,
    ),
    "slack": TentacleSetup(
        label="Slack", configure=partial(configure_channel, "slack")
    ),
    "lark": TentacleSetup(label="Lark", configure=partial(configure_channel, "lark")),
    "discord": TentacleSetup(
        label="Discord", configure=partial(configure_channel, "discord")
    ),
}


def channels_step(channel: list[str] | None, *, console: Console) -> list[str]:
    console.print("4/7 · Channels Tentacles", style=f"bold {brand_color}")
    channels = select_tentacles(
        CHANNELS,
        [] if channel == ["none"] else channel,
        console=console,
        prompt="Select channel tentacles (optional)",
        invalid_message="Supported --channel values: slack, lark, discord, trunkline; use none alone for no channels.",
    )
    console.print(
        "Follow CONFIGURATION.md in the installation to complete configuration."
    )
    return channels
