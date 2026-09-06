"""The root Typer app behind the `octomate` command: each agent's command group,
configure, serve, and upgrade.

Every group is mounted whatever the machine holds — server or client — so the surface
stays discoverable; a command that needs the absent half says so when invoked rather
than vanishing.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from octomate_cli.config import configure
from octomate_cli.serve import serve, upgrade
from octomate_cli.tentacles.claude import claude_typer
from octomate_cli.tentacles.codex import codex_typer
from octomate_cli.tentacles.deepseek import deepseek_typer

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.command("serve")(serve)
app.command("upgrade")(upgrade)
# One credential per person, written here and read by every agent's hooks, tail and
# MCP entry alike.
app.command("configure")(configure)
app.add_typer(claude_typer, name="claude")
app.add_typer(codex_typer, name="codex")
app.add_typer(deepseek_typer, name="deepseek")


@app.callback(invoke_without_command=True)
def options(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version", is_eager=True, help="Show installed package versions."
        ),
    ] = False,
) -> None:
    if show_version:
        for package in ("octomate-cli", "octomate-protocol", "octomate"):
            try:
                installed = version(package)
            except PackageNotFoundError:
                continue
            typer.echo(f"{package} {installed}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
