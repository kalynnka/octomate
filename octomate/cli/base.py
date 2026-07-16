"""The root Typer app: it mounts each agent tentacle's command group.

Every tentacle's group is mounted so the operator surface is discoverable regardless of
what this deployment configures; a group whose tentacle is not configured hints as much
when invoked (see each tentacle's `typer` module) rather than vanishing. `octomate claude
...` today, `octomate codex ...` next.
"""

from __future__ import annotations

import typer

from octomate.tentacles.agent.claude.typer import claude_typer

app = typer.Typer(help="Octomate operator CLI.", no_args_is_help=True)
app.add_typer(claude_typer, name="claude")


if __name__ == "__main__":
    app()
