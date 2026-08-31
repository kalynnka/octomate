"""What every agent's hook installer shares: the check that the credential the hooks
will carry — the user's own secret, from their `users:` entry — actually resolves,
and the scripts the installed hooks run.
"""

from __future__ import annotations

from pathlib import Path

import typer

from octomate_cli.config import CLISettings, cli_settings, user_config_path

# The forwarding command hook's script — it carries the event body from stdin to the
# hook router over HTTP. Both agents' installers write commands that run it by
# absolute path, so a hook never imports the packages (see its module docstring).
EMIT_SCRIPT = Path(__file__).with_name("emit.py")

# The launcher command hook's script — it spawns the session's transcript tail
# detached. Run by absolute path for the same reason.
LAUNCH_SCRIPT = Path(__file__).with_name("launch.py")


def announce_secret() -> None:
    """Warn when hooks were just installed against a credential that resolves to
    nothing: the install reports success, and every turn after it 401s."""
    if cli_settings().secret is None:
        typer.secho(
            f"\nNo credential found — neither ./.octomate/cli.toml nor "
            f"{user_config_path()} holds one, and ${CLISettings.env('secret')} is "
            "unset. Run `octomate configure`, or every hook will be refused.",
            fg=typer.colors.YELLOW,
            err=True,
        )
