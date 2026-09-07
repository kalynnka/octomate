"""Command registration for the claude tentacle; stream clients load only when tailing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.tentacles.claude.hooks import (
    CLAUDE_HOOK_PATH,
    CLAUDE_STREAM_PATH,
    hooks_typer,
    stream_url_for,
)
from octomate_cli.tentacles.claude.mcp import mcp_typer

claude_typer = typer.Typer(
    help="Operate the native Claude Code integration.", no_args_is_help=True
)
claude_typer.add_typer(hooks_typer, name="hooks")
claude_typer.add_typer(mcp_typer, name="mcp")


@claude_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path, typer.Option(help="The session's transcript path on this machine.")
    ],
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/claude/stream); "
            f"defaults to one derived from ${CLISettings.env('url')}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
) -> None:
    """Stream a native session's transcript to Octomate, raw lines over one socket.

    Spawned per session by the launcher hook; safe to run by hand for a backfill —
    the server states where each file resumes, so re-running never duplicates. Reads
    the hook credential — and, absent `--url`, the server's address — from the
    environment, like every hook client does.
    """
    if url is None:
        base = cli_settings().url
        if base is None:
            raise typer.BadParameter(
                f"no --url given, {CLISettings.env('url')} is unset, and no cli.toml "
                "names a url — one of them must say where Octomate is"
            )
        url = stream_url_for(base.rstrip("/") + CLAUDE_HOOK_PATH)
    from octomate_cli.streaming.files import (
        main,  # watchfiles/websockets; only when tailing
    )

    main(session_id=session, transcript_path=path, url=url, cwd=cwd)


__all__ = ["CLAUDE_HOOK_PATH", "CLAUDE_STREAM_PATH", "claude_typer"]
