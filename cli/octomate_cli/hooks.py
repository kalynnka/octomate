"""What every agent's hook installer shares: the credential the hooks carry — the
deployment's one secret, which `octomate secret` hands to a shell — and the scripts
the installed hooks run.
"""

from __future__ import annotations

import secrets
import shlex
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from octomate_cli.config import SECRET_ENV, resolved_secret, user_config_path

# Everything for a person, so stdout stays the bare export line `eval` and `>>` consume.
console = Console(stderr=True)

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
    if resolved_secret() is None:
        typer.secho(
            f"\nNo credential found — {SECRET_ENV} is unset and neither "
            f"./.octomate/cli.toml nor {user_config_path()} holds one. Run "
            "`octomate configure`, or every hook will be refused.",
            fg=typer.colors.YELLOW,
            err=True,
        )


def secret() -> None:
    """Print Octomate's credential as a shell export line, generating one if unset.

    Prints what the client itself resolves — the environment, then the config file —
    and hands over the line, leaving the placing to you: `eval "$(octomate secret)"`.
    For a durable home that survives GUI launches, prefer `octomate configure`, which
    writes the config file instead.

    A resolving secret is printed as-is, never rotated — re-running is what someone
    does when hooks already work. A generated one exists nowhere yet, and stderr says
    so. Only the export line goes to stdout, so it stays eval'able.
    """
    configured = resolved_secret()
    token = configured if configured is not None else secrets.token_urlsafe(32)
    # Prose is left for rich to wrap; lines meant to be copied are kept short enough to
    # survive intact, since a token split across two lines is worse than no help at all.
    body = [
        "The line below sets [bold]this shell[/]. To keep it:",
        "",
        "  [cyan]octomate secret >> ~/.zshrc[/]  [dim](or ~/.zshenv)[/]",
        "",
        "[dim]An environment is captured at process start — restart shells, and VSCode, "
        "before their sessions carry it.[/]",
    ]
    if configured is None:
        # A generated token is url-safe, and a resolving one never reaches this panel,
        # so no value here can carry markup.
        body = [
            "[yellow]Octomate cannot see this one[/] — its routers will refuse the hooks "
            "until it can. On this machine, prefer the client config file:",
            "",
            f"  [cyan]octomate configure --secret [green]{token}[/][/]",
            "",
            "And give the server the same, in whichever you keep secrets in:",
            "",
            "  [dim]octomate.yaml[/]",
            "    octomate:",
            f"      secret: [green]{token}[/]",
            "",
            "  [dim].env[/]",
            f"    {SECRET_ENV}=[green]{token}[/]",
            "",
            *body,
        ]
    console.print(
        Panel(
            "\n".join(body),
            title="[bold]credential[/]"
            if configured is not None
            else "[bold yellow]new credential[/]",
            border_style="cyan" if configured is not None else "yellow",
            padding=(1, 2),
        )
    )
    # Quoted because a shell parses this line: a hand-written secret can hold spaces or
    # `$`, which unquoted would export some *other* value and 401 later.
    typer.echo(f"export {SECRET_ENV}={shlex.quote(token)}")
