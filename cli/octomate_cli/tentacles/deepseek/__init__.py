"""Command registration for the deepseek tentacle; stream clients load only when tailing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.tentacles.deepseek.hooks import (
    DEEPSEEK_HOOK_PATH,
    DEEPSEEK_STREAM_PATH,
    hooks_typer,
    stream_url_for,
)
from octomate_cli.tentacles.deepseek.mcp import mcp_typer

# Where this machine's dsh gateway answers, resolved at fire time: the
# environment names it when the operator moved the port, else dsh's default
# bind — the same default `agents.deepseek.port` carries server-side.
DSH_URL_ENV = "DSH_API_URL"


DEFAULT_DSH_URL = "http://127.0.0.1:3080"


deepseek_typer = typer.Typer(
    help="Operate the native dsh (DeepSeek Harness) integration.",
    no_args_is_help=True,
)
deepseek_typer.add_typer(hooks_typer, name="hooks")
deepseek_typer.add_typer(mcp_typer, name="mcp")


@deepseek_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path,
        typer.Option(
            help="The session log's path on this machine — a recorded label; "
            "the tail reads the gateway, never this file."
        ),
    ] = Path(""),
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/deepseek/stream); "
            f"defaults to one derived from ${CLISettings.env('url')}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
    dsh_url: Annotated[
        str | None,
        typer.Option(
            help="This machine's dsh gateway; defaults to "
            f"${DSH_URL_ENV} or {DEFAULT_DSH_URL}."
        ),
    ] = None,
) -> None:
    """Stream a native dsh session's events to Octomate, read from this
    machine's dsh gateway rather than the zstd-framed log file.

    Spawned per session by the launcher hook; safe to run by hand for a
    backfill — the server states where the session resumes, so re-running
    never duplicates. Reads the hook credential — and, absent `--url`, the
    server's address — from the environment, like every hook client does.
    """
    if url is None:
        base = cli_settings().url
        if base is None:
            raise typer.BadParameter(
                f"no --url given, {CLISettings.env('url')} is unset, and no cli.toml "
                "names a url — one of them must say where Octomate is"
            )
        url = stream_url_for(base.rstrip("/") + DEEPSEEK_HOOK_PATH)
    if dsh_url is None:
        dsh_url = os.environ.get(DSH_URL_ENV) or DEFAULT_DSH_URL
    from octomate_cli.streaming.deepseek import main  # websockets; only when tailing

    main(
        session_id=session,
        transcript_path=path,
        url=url,
        cwd=cwd,
        dsh_url=dsh_url,
    )


__all__ = ["DEEPSEEK_HOOK_PATH", "DEEPSEEK_STREAM_PATH", "deepseek_typer"]
