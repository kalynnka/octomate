"""Prepare a habitat backend independently of agent credentials."""

import platform
import shutil
import subprocess
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
    srt = "srt"


SBX_AGENTS = frozenset({"claude", "codex", "deepseek", "acp", "inkling"})


def parse_agent_habitats(
    agents: list[str], assignments: list[str]
) -> dict[str, HabitatMode]:
    """Parse unique backend assignments to selected agents."""
    selections: dict[str, HabitatMode] = {}
    for assignment in assignments:
        agent, separator, backend = assignment.partition("=")
        if not separator or agent not in agents:
            raise typer.BadParameter("--agent-habitat requires a selected AGENT=MODE.")
        if agent in selections:
            raise typer.BadParameter(f"Choose the habitat for {agent} only once.")
        try:
            selections[agent] = HabitatMode(backend)
        except ValueError as error:
            raise typer.BadParameter("Habitat must be native, srt or sbx.") from error
    return selections


@habitat_typer.command()
def setup(
    backend: Annotated[
        Literal["sbx", "srt"], typer.Argument(help="Habitat backend to set up.")
    ],
    check: Annotated[
        bool,
        typer.Option("--check", help="Verify host setup without prompts or changes."),
    ] = False,
) -> None:
    """Check SRT installation or sbx host access independently of credentials."""
    if backend == "srt":
        if platform.system() != "Darwin":
            raise typer.BadParameter(
                "Octomate's SRT habitat is currently qualified for macOS only."
            )
        executable = shutil.which("srt")
        if executable is None:
            raise typer.BadParameter(
                "Install @anthropic-ai/sandbox-runtime with npm and put srt on the service account's PATH."
            )
        node = shutil.which("node")
        if node is None:
            raise typer.BadParameter("Install Node.js on the service account's PATH.")
        sdk = Path(executable).resolve().with_name("index.js")
        if not sdk.is_file():
            raise typer.BadParameter(
                "srt must resolve to the @anthropic-ai/sandbox-runtime npm entry point."
            )
        result = subprocess.run(
            [
                node,
                str(Path(__file__).with_name("srt.mjs")),
                sdk.as_uri(),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise typer.BadParameter(
                f"SRT dependency check failed: {result.stderr.strip()}"
            )
        if result.stderr.strip():
            console.print(result.stderr.strip(), markup=False)
        console.print(
            "SRT dependencies checked. Runtime isolation is checked when a habitat starts."
        )
        return
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
