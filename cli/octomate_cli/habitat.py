"""Prepare a service-wide habitat independently of agent credentials."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.prompt import Confirm

from octomate_cli import sbx
from octomate_cli.credentials import setup as setup_credentials

console = Console(stderr=True)
habitat_typer = typer.Typer(help="Prepare the service account's agent habitats.")
habitat_typer.command("credentials")(setup_credentials)


class HabitatMode(StrEnum):
    native = "native"
    sbx = "sbx"


SBX_AGENTS = frozenset({"claude", "codex", "deepseek", "acp", "inkling"})


@habitat_typer.command()
def setup(
    backend: Annotated[
        Literal["sbx"], typer.Argument(help="Habitat backend to set up.")
    ],
    check: Annotated[
        bool,
        typer.Option("--check", help="Verify host setup without prompts or changes."),
    ] = False,
) -> None:
    """Check sbx, Docker login, daemon access and SSH forwarding policy."""
    executable = sbx.executable()
    console.print(f"sbx host setup for {Path.home()}")
    if not check and Confirm.ask(
        "Sign in to Docker with sbx now?", default=False, console=console
    ):
        sbx.run_sbx(executable, "login")
    sbx.run_sbx(executable, "ls", "--quiet", capture=True)
    forwarding = sbx.run_sbx(
        executable, "settings", "get", "ssh.agentForwardingEnabled", capture=True
    )
    if forwarding.strip() != "false":
        raise typer.BadParameter(
            "Octomate requires SSH agent forwarding off because it would let "
            "processes in users' habitats authenticate with SSH identities loaded "
            "in the host service account's agent. Those identities must not be "
            "shared across habitat users. Run "
            "`sbx settings set ssh.agentForwardingEnabled false`, then "
            "`sbx daemon restart` before setup."
        )
    console.print(
        "sbx host setup is ready. Provider credentials are configured separately."
    )
