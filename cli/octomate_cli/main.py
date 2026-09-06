"""The root Typer app behind the `octomate` command: each agent's command group,
configure, serve, and upgrade.

Every group is mounted whatever the machine holds — server or client — so the surface
stays discoverable; a command that needs the absent half says so when invoked rather
than vanishing.
"""

from __future__ import annotations

import typer

from octomate_cli.claude import claude_typer
from octomate_cli.codex import codex_typer
from octomate_cli.config import configure
from octomate_cli.deepseek import deepseek_typer
from octomate_cli.serve import serve, upgrade

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.command("serve")(serve)
app.command("upgrade")(upgrade)
# One credential per person, written here and read by every agent's hooks, tail and
# MCP entry alike.
app.command("configure")(configure)
app.add_typer(claude_typer, name="claude")
app.add_typer(codex_typer, name="codex")
app.add_typer(deepseek_typer, name="deepseek")


if __name__ == "__main__":
    app()
