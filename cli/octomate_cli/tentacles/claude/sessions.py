"""Prepare an uploaded Claude transcript inside its destination habitat."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID

import httpx2
import typer

from octomate_cli.config import cli_settings
from octomate_cli.tentacles.claude.hooks import CLAUDE_HOOK_PATH

habitat_typer = typer.Typer(
    help="Run Claude operations inside a habitat.", no_args_is_help=True
)


@habitat_typer.command()
def relocate(
    session: UUID, conversation: UUID, end: Annotated[int, typer.Argument(min=1)]
) -> None:
    """Download through the last complete turn and fork it in this workspace."""
    # The SDK is installed by the optional Claude extra.
    from claude_agent_sdk import fork_session, project_key_for_directory

    settings = cli_settings()
    if not settings.url or not settings.token:
        raise typer.BadParameter("The habitat needs the core URL and a hook credential")
    url = f"{settings.url.rstrip('/')}{CLAUDE_HOOK_PATH}/transcripts/{conversation}"
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    project = Path("projects") / project_key_for_directory()
    destination = config / project
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=config) as directory:
        staged = Path(directory) / project
        staged.mkdir(parents=True)
        with (
            httpx2.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {settings.token}"},
                timeout=60,
            ) as response,
            (staged / f"{session}.jsonl").open("wb") as output,
        ):
            response.raise_for_status()
            output.writelines(response.iter_bytes())
            if output.tell() < end:
                raise ValueError("The transcript ends before the selected turn")
            output.truncate(end)
        os.environ["CLAUDE_CONFIG_DIR"] = directory
        try:
            forked = fork_session(str(session), directory=str(Path.cwd())).session_id
        finally:
            os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        (staged / f"{forked}.jsonl").rename(destination / f"{forked}.jsonl")
    typer.echo(forked)
