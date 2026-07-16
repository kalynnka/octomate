"""The root Typer app: it mounts each agent tentacle's command group.

Every group is mounted whatever this deployment configures, so the operator surface stays
discoverable; an unconfigured tentacle's group hints as much when invoked rather than
vanishing.
"""

from __future__ import annotations

import typer

from octomate.tentacles.agent.claude.typer import claude_typer
from octomate.tentacles.agent.codex.typer import codex_typer
from octomate.tentacles.agent.typer import hooks_typer

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.add_typer(claude_typer, name="claude")
app.add_typer(codex_typer, name="codex")
# Cross-tentacle: the hook credential is one secret every agent's router shares.
app.add_typer(hooks_typer, name="hooks")


if __name__ == "__main__":
    app()
