"""`octomate serve` — run the API.

Kept out of `base.py` so that module stays assembly, the way each agent tentacle
contributes its group from its own module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

# Config, the database and the agent working directories are all resolved relative
# to the process's working directory, so serving is already a from-here operation
# and the factory can be named as a plain import path.
APP = "main:create_app"


def serve(
    host: Annotated[
        str | None,
        typer.Option(help="Address to bind. Defaults to octomate.host (127.0.0.1)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(help="Port to bind. Defaults to octomate.port (8000)."),
    ] = None,
    reload: Annotated[
        bool,
        typer.Option(help="Restart when files under octomate/ change."),
    ] = False,
    tmux: Annotated[
        bool,
        typer.Option(help="Serve inside a detached tmux session and attach to it."),
    ] = False,
    session: Annotated[
        str,
        typer.Option(help="tmux session name."),
    ] = "octomate",
) -> None:
    """Serve the Octomate API.

    Octomate is meant to outlive the terminal that starts it: channels hold their
    sockets open, and the transcript tailers keep watching for native sessions
    started somewhere else entirely. `--tmux` is that shape without a service
    manager — it creates the session if it is missing, and otherwise just attaches
    to the one already serving.
    """
    if tmux:
        if shutil.which("tmux") is None:
            typer.secho("tmux is not installed.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        serving = subprocess.run(
            ["tmux", "has-session", "-t", session], capture_output=True
        )
        if serving.returncode != 0:
            command = [
                # Resolved: tmux starts the command from a shell that never activated
                # the virtualenv, and this script's shebang is what points back into it.
                str(Path(sys.argv[0]).resolve()),
                "serve",
            ]
            if host is not None:
                command += ["--host", host]
            if port is not None:
                command += ["--port", str(port)]
            if reload:
                command.append("--reload")
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-c",
                    os.getcwd(),
                    *command,
                ],
                check=True,
            )
            typer.secho(f"Serving in tmux session {session!r}.", fg=typer.colors.GREEN)
        # tmux refuses a nested attach, and switching is what the request means
        # when it comes from inside a pane.
        attach = "switch-client" if os.environ.get("TMUX") else "attach-session"
        subprocess.run(["tmux", attach, "-t", session], check=True)
        return

    import uvicorn

    from octomate.config import OctomateConfig  # heavy; only when the CLI serves

    config = OctomateConfig()
    uvicorn.run(
        APP,
        factory=True,
        host=str(config.host) if host is None else host,
        port=config.port if port is None else port,
        reload=reload,
        # Only the package. The working directory also holds `.octomate/`, whose
        # SQLite database is written on every turn and would restart the server
        # out from under itself; editing `main.py` needs a manual restart.
        reload_dirs=["octomate"],
        log_level=config.logging.level.lower(),
    )
