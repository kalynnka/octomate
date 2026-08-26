"""The root Typer app behind the `octomate` command: each agent's command group, the
credential, configure, and serve.

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
from octomate_cli.hooks import secret
from octomate_cli.serve import serve

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.command("serve")(serve)
app.command("configure")(configure)
# Cross-tentacle: one secret every agent's hook router, and every MCP server, shares.
app.command("secret")(secret)
app.add_typer(claude_typer, name="claude")
app.add_typer(codex_typer, name="codex")
app.add_typer(deepseek_typer, name="deepseek")


if __name__ == "__main__":
    app()
