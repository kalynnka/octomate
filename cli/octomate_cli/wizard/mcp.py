from __future__ import annotations

import typer
from pydantic import TypeAdapter
from rich.console import Console
from rich.prompt import Confirm, Prompt

from octomate_cli.mcp import McpPreset
from octomate_cli.wizard.base import TentacleSetup, brand_color, select_tentacles


def github(console: Console) -> McpPreset:
    console.print(
        "GitHub uses an OAuth App with Device Flow enabled. "
        "Users install this tentacle and authorize their own accounts later."
    )
    name = Prompt.ask("Tentacle name", default="github", console=console)
    client_id = Prompt.ask("GitHub OAuth application client ID", console=console)
    if not name.strip() or not client_id.strip():
        raise typer.BadParameter("Client ID and tentacle name must not be empty.")
    read_only = Confirm.ask(
        "Use GitHub's read-only MCP endpoint?", default=False, console=console
    )
    selection = McpPreset(
        provider="github", name=name, client_id=client_id, read_only=read_only
    )
    scopes = TypeAdapter(list[str]).validate_python(selection.configuration()["scopes"])
    console.print(f"OAuth scopes: {', '.join(scopes)}")
    if read_only:
        console.print(
            "OAuth scopes still permit writes; the MCP endpoint limits tools to reads."
        )
    return selection


MCPS: dict[str, TentacleSetup[McpPreset]] = {
    "github": TentacleSetup(label="GitHub", configure=github),
}


def mcps_step(*, interactive: bool, console: Console) -> list[McpPreset]:
    console.print("5/7 · MCP Tentacles", style=f"bold {brand_color}")
    if not interactive:
        console.print("Optional MCP setup skipped with --yes.")
        return []
    return select_tentacles(
        MCPS,
        None,
        console=console,
        prompt="Select MCP tentacles (optional)",
        invalid_message="Choose a supported MCP preset: github.",
    )
