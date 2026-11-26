"""Command registration for the claude tentacle; stream clients load only when tailing."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from octomate_protocol.hooks import NativeHookEvent

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.tentacles.claude.hooks import (
    CLAUDE_HOOK_PATH,
    CLAUDE_STREAM_PATH,
    hooks_typer,
    stream_url_for,
)
from octomate_cli.tentacles.claude.mcp import mcp_typer
from octomate_cli.tentacles.claude.sessions import habitat_typer
from octomate_cli.transcripts import upload_transcripts

claude_typer = typer.Typer(
    help="Operate the native Claude Code integration.", no_args_is_help=True
)
claude_typer.add_typer(hooks_typer, name="hooks")
claude_typer.add_typer(mcp_typer, name="mcp")
claude_typer.add_typer(habitat_typer, name="habitat")


@claude_typer.command("upload")
def upload(
    conversation: Annotated[
        UUID, typer.Option(help="The conversation the session is driven for.")
    ],
) -> None:
    """Upload the transcript named by Claude's hook payload and its subagent files."""
    event = NativeHookEvent.model_validate_json(sys.stdin.read())
    if event.hook_event_name == "SubagentStop":
        if not event.agent_id or not event.agent_transcript_path:
            raise typer.BadParameter(
                "SubagentStop requires agent_id and agent_transcript_path"
            )
        path = None
        subagents = {event.agent_id: Path(event.agent_transcript_path)}
    else:
        if not event.transcript_path:
            raise typer.BadParameter("hook input requires transcript_path")
        path = Path(event.transcript_path)
        # A child's final line can land after its SubagentStop hook.
        children = path.with_suffix("") / "subagents"
        subagents = {
            child.stem.removeprefix("agent-"): child
            for child in children.glob("agent-*.jsonl")
        }
    asyncio.run(
        upload_transcripts(
            conversation, path, subagents=subagents, hook_path=CLAUDE_HOOK_PATH
        )
    )


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

    main(agent="claude", session_id=session, transcript_path=path, url=url, cwd=cwd)


__all__ = ["CLAUDE_HOOK_PATH", "CLAUDE_STREAM_PATH", "claude_typer"]
