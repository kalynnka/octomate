"""Select tentacles together, then configure each selected tentacle."""

from __future__ import annotations

import os
from dataclasses import dataclass

import questionary
import typer
from prompt_toolkit.output import ColorDepth
from pydantic import TypeAdapter
from rich.console import Console

from octomate_cli.mcp import McpPreset
from octomate_cli.wizard.agents import AGENTS
from octomate_cli.wizard.base import TentacleSetup, brand_color, selection_style
from octomate_cli.wizard.channels import CHANNELS
from octomate_cli.wizard.mcp import MCPS

TENTACLES: dict[str, TentacleSetup[str] | TentacleSetup[McpPreset]] = {
    **AGENTS,
    **CHANNELS,
    **MCPS,
}


@dataclass
class TentacleSelections:
    agents: list[str]
    channels: list[str]
    mcps: list[McpPreset]


def select_tentacles(agent: list[str] | None, channel: list[str] | None) -> list[str]:
    choices: list[questionary.Choice] = []
    for name, setup in TENTACLES.items():
        checked = setup.checked
        if name in AGENTS and agent is not None:
            checked = name in agent
        elif name in CHANNELS and channel is not None:
            checked = name in channel
        choices.append(
            questionary.Choice(
                f"{setup.label} [{' · '.join(setup.capabilities)}]",
                value=name,
                checked=checked,
            )
        )
    answer = questionary.checkbox(
        "Select tentacles",
        choices=choices,
        style=selection_style,
        instruction="(↑/↓ move · Space select · Enter continue)",
        validate=lambda selected: (
            any(name in AGENTS for name in selected)
            or "Select at least one agent tentacle."
        ),
        color_depth=ColorDepth.DEPTH_1_BIT
        if "NO_COLOR" in os.environ
        else ColorDepth.TRUE_COLOR,
    ).ask()
    if answer is None:
        raise typer.Abort()
    return TypeAdapter(list[str]).validate_python(answer)


def tentacles_step(
    agent: list[str] | None,
    channel: list[str] | None,
    *,
    interactive: bool,
    console: Console,
) -> TentacleSelections:
    console.print("3/6 · Tentacles", style=f"bold {brand_color}")
    if agent is not None and (not agent or any(name not in AGENTS for name in agent)):
        raise typer.BadParameter(
            "Choose at least one supported --agent: claude, codex, deepseek (DSH; experimental)."
        )
    if channel == ["none"]:
        channel = []
    if channel is not None and any(name not in CHANNELS for name in channel):
        raise typer.BadParameter(
            "Supported --channel values: slack, lark, discord, trunkline; use none alone for no channels."
        )
    selected = (
        select_tentacles(agent, channel)
        if interactive
        else [*(agent or []), *(channel or [])]
    )
    if any(name not in TENTACLES for name in selected):
        raise typer.BadParameter("Choose supported tentacles.")
    if not any(name in AGENTS for name in selected):
        raise typer.BadParameter("Select at least one agent tentacle.")
    selected = list(dict.fromkeys(selected))
    console.print("4/6 · Tentacle details", style=f"bold {brand_color}")
    selections = TentacleSelections(agents=[], channels=[], mcps=[])
    for index, name in enumerate(selected, start=1):
        setup = TENTACLES[name]
        console.print(f"{index}/{len(selected)} · {setup.label}", style="bold")
        match setup.configure(console):
            case McpPreset() as mcp:
                selections.mcps.append(mcp)
            case str() as agent_id if name in AGENTS:
                selections.agents.append(agent_id)
            case str() as channel_id:
                selections.channels.append(channel_id)
    return selections
