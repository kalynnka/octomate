from __future__ import annotations

import typer
from pydantic import TypeAdapter
from rich.console import Console
from rich.prompt import Confirm, Prompt

from octomate_cli.mcp import McpPreset
from octomate_cli.wizard.base import TentacleSetup


def github(console: Console) -> McpPreset:
    console.print(
        "GitHub offers device and browser authorization using an OAuth App with Device Flow enabled. "
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
    console.print(
        f"Set OCTOMATE__TENTACLES__{selection.name.upper()}__CLIENT_SECRET in the generated .env. "
        "Register the browser callback URL printed after preparation in the GitHub OAuth App."
    )
    scopes = TypeAdapter(list[str]).validate_python(selection.configuration()["scopes"])
    console.print(f"OAuth scopes: {', '.join(scopes)}")
    if read_only:
        console.print(
            "OAuth scopes still permit writes; the MCP endpoint limits tools to reads."
        )
    return selection


MCPS: dict[str, TentacleSetup[McpPreset]] = {
    "github": TentacleSetup(label="GitHub", capabilities=("MCP",), configure=github),
}
