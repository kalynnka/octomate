"""Command registration for the codex tentacle; stream clients load only when tailing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.tentacles.codex.hooks import (
    CODEX_HOOK_PATH,
    CODEX_STREAM_PATH,
    hooks_typer,
    stream_url_for,
)
from octomate_cli.tentacles.codex.mcp import mcp_typer

codex_typer = typer.Typer(
    help="Operate the native Codex integration.", no_args_is_help=True
)
codex_typer.add_typer(hooks_typer, name="hooks")
codex_typer.add_typer(mcp_typer, name="mcp")


@codex_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path, typer.Option(help="The session's rollout path on this machine.")
    ],
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/codex/stream); "
            f"defaults to one derived from ${CLISettings.env('url')}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
    agent_path: Annotated[
        Path | None,
        typer.Option(
            help="A child rollout to spool for the session's tail — what the "
            "launcher passes through from a SubagentStop hook."
        ),
    ] = None,
) -> None:
    """Stream a native Codex session's rollout to Octomate, raw lines over one socket.

    Spawned per session by the launcher hook; safe to run by hand for a backfill —
    committed turns are skipped server-side, so re-running never duplicates. Reads
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
        url = stream_url_for(base.rstrip("/") + CODEX_HOOK_PATH)
    from octomate_cli.streaming.files import (  # watchfiles; only when tailing
        main,
        spool_path,
    )

    main(
        session_id=session,
        transcript_path=path,
        url=url,
        cwd=cwd,
        spool=spool_path(session),
        agent_path=agent_path,
    )


__all__ = ["CODEX_HOOK_PATH", "CODEX_STREAM_PATH", "codex_typer"]
