"""The root Typer app behind the `octomate` command: each agent's command group, the
hook credential group, and serve.

Every group is mounted whatever the machine holds — server or client — so the surface
stays discoverable; a command that needs the absent half says so when invoked rather
than vanishing.
"""

from __future__ import annotations

import typer

from octomate_cli.claude import claude_typer
from octomate_cli.codex import codex_typer
from octomate_cli.config import configure
from octomate_cli.hooks import hooks_typer
from octomate_cli.serve import serve

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.command("serve")(serve)
app.command("configure")(configure)
app.add_typer(claude_typer, name="claude")
app.add_typer(codex_typer, name="codex")
# Cross-tentacle: the hook credential is one secret every agent's router shares.
app.add_typer(hooks_typer, name="hooks")


if __name__ == "__main__":
    app()
