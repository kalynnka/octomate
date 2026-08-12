"""`octomate hooks ...` — the credential native-session hooks authenticate with.

Owned at the agent level rather than by either tentacle: every agent's hook router
authenticates against the same secret.
"""

from __future__ import annotations

import secrets
import shlex

import typer
from rich.console import Console
from rich.panel import Panel

# Everything for a person, so stdout stays the bare export line `eval` and `>>` consume.
console = Console(stderr=True)

# How a *client* carries the credential. A client-side contract, so it lives with the
# installer that writes it; the app itself reads `OctomateConfig.hook_secret` and does
# not care which source filled it. `cli/emit.py` repeats the name as a literal because
# it cannot import the package — change both together.
HOOK_SECRET_ENV = "OCTOMATE__HOOK_SECRET"

hooks_typer = typer.Typer(
    help="Manage the credential native-session hooks authenticate with.",
    no_args_is_help=True,
)


def announce_hook_secret() -> None:
    """Warn when hooks were just installed against a secret that does not exist: the
    install reports success, and every turn after it 401s."""
    from octomate.config import OctomateConfig  # heavy; only when the CLI installs

    if OctomateConfig().hook_secret is None:
        typer.secho(
            "\nNo hook secret configured — these hooks will be refused. "
            "Run `octomate hooks secret`.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@hooks_typer.command("secret")
def secret() -> None:
    """Print the hook credential as a shell export line, generating one if unset.

    A session reads the secret from the environment only, and is a separate process that
    never sees Octomate's config. So this hands over the line and leaves the placing to
    you: `eval "$(octomate hooks secret)"`.

    A configured secret is printed as-is, never rotated — re-running is what someone does
    when hooks already work. A generated one exists nowhere Octomate can see, and stderr
    says so. Only the export line goes to stdout, so it stays eval'able.
    """
    from octomate.config import OctomateConfig  # heavy; only when the CLI asks

    configured = OctomateConfig().hook_secret
    token = (
        configured.get_secret_value()
        if configured is not None
        else secrets.token_urlsafe(32)
    )
    # Prose is left for rich to wrap; lines meant to be copied are kept short enough to
    # survive intact, since a token split across two lines is worse than no help at all.
    body = [
        "The line below sets [bold]this shell[/]. To keep it:",
        "",
        "  [cyan]octomate hooks secret >> ~/.zshrc[/]  [dim](or ~/.zshenv)[/]",
        "",
        "[dim]An environment is captured at process start — restart shells, and VSCode, "
        "before their sessions carry it.[/]",
    ]
    if configured is None:
        # A generated token is url-safe, and a configured one never reaches this panel,
        # so no value here can carry markup.
        body = [
            "[yellow]Octomate cannot see this one[/] — its routers will refuse the hooks "
            "until it can. Put it in whichever you keep secrets in:",
            "",
            "  [dim]octomate.yaml[/]",
            "    octomate:",
            f"      hook_secret: [green]{token}[/]",
            "",
            "  [dim].env[/]",
            f"    {HOOK_SECRET_ENV}=[green]{token}[/]",
            "",
            *body,
        ]
    console.print(
        Panel(
            "\n".join(body),
            title="[bold]hook credential[/]"
            if configured is not None
            else "[bold yellow]new hook credential[/]",
            border_style="cyan" if configured is not None else "yellow",
            padding=(1, 2),
        )
    )
    # Quoted because a shell parses this line: a hand-written secret can hold spaces or
    # `$`, which unquoted would export some *other* value and 401 later.
    typer.echo(f"export {HOOK_SECRET_ENV}={shlex.quote(token)}")
