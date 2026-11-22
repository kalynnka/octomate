"""Prepare an uploaded Claude transcript inside its destination habitat."""

import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID

import typer

habitat_typer = typer.Typer(
    help="Run Claude operations inside a habitat.", no_args_is_help=True
)


@habitat_typer.command()
def relocate(
    session: UUID,
    transcript: Annotated[bool, typer.Option("--transcript")] = False,
) -> None:
    """Fork a habitat session, optionally reading retained JSONL from stdin."""
    # The SDK is installed by the optional Claude extra.
    from claude_agent_sdk import fork_session, project_key_for_directory

    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    project = Path("projects") / project_key_for_directory()
    destination = config / project
    destination.mkdir(parents=True, exist_ok=True)
    if not transcript:
        forked = fork_session(str(session)).session_id
        [forked_path] = destination.parent.glob(f"*/{forked}.jsonl")
        forked_path.rename(destination / forked_path.name)
        typer.echo(forked)
        return
    with TemporaryDirectory(dir=config) as directory:
        staged = Path(directory) / project
        staged.mkdir(parents=True)
        with (staged / f"{session}.jsonl").open("wb") as output:
            shutil.copyfileobj(sys.stdin.buffer, output)
        os.environ["CLAUDE_CONFIG_DIR"] = directory
        try:
            forked = fork_session(str(session), directory=str(Path.cwd())).session_id
        finally:
            os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        (staged / f"{forked}.jsonl").rename(destination / f"{forked}.jsonl")
    typer.echo(forked)
