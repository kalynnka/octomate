"""Run the API directly or manage its launchd service through a plist definition."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from octomate_protocol.deployment import DatabaseBackup
from pydantic import BaseModel, ConfigDict, Field

# Config, the database and the agent working directories are all resolved relative
# to the process's working directory, so serving is already a from-here operation
# and the factory can be named as a plain import path.
APP = "main:create_app"


PlistFile = Annotated[
    Path,
    typer.Option(
        "--plist",
        exists=True,
        dir_okay=False,
        help="Existing launchd service plist. Required even when using the default path.",
    ),
]
DEFAULT_PLIST = Path("/Library/LaunchDaemons/io.octomate.server.plist")


class PlistService(BaseModel):
    """A launchd job and its runtime configuration, loaded from a service plist."""

    model_config = ConfigDict(hide_input_in_errors=True)

    label: str = Field(alias="Label", pattern=r"^[a-zA-Z0-9_.-]+$")
    directory: Path = Field(alias="WorkingDirectory")
    user: str = Field(alias="UserName")
    arguments: list[str] = Field(alias="ProgramArguments")
    environment: dict[str, str] = Field(alias="EnvironmentVariables", repr=False)
    program: None = Field(default=None, alias="Program")
    keep_alive: Literal[True] = Field(alias="KeepAlive")
    abandon_process_group: Literal[False] = Field(
        default=False, alias="AbandonProcessGroup"
    )

    @property
    def target(self) -> str:
        """The system launchd job identified by the plist's Label."""
        return f"system/{self.label}"

    @property
    def process_environment(self) -> dict[str, str]:
        """Environment for commands running in the plist's configured checkout."""
        return {
            "HOME": str(Path.home()),
            "USER": self.user,
            "LOGNAME": self.user,
            **self.environment,
        }

    def loaded(self) -> bool:
        """Check whether the launchd job defined by this plist is loaded."""
        result = subprocess.run(
            ["/bin/launchctl", "print", self.target], capture_output=True, text=True
        )
        if result.returncode == 113:
            return False
        result.check_returncode()
        return True

    def launchctl(self, action: str, *arguments: str) -> None:
        """Apply a privileged launchd action while managing the plist's service."""
        subprocess.run(
            ["/usr/bin/sudo", "/bin/launchctl", action, *arguments], check=True
        )

    def maintenance(self, action: str, backup: DatabaseBackup | None = None) -> str:
        """Run maintenance with the plist's working directory and environment."""
        result = subprocess.run(
            [
                str(self.directory / ".venv/bin/python"),
                "-m",
                "octomate.deployment",
                action,
            ],
            cwd=self.directory,
            env=self.process_environment,
            input=backup.model_dump_json() if backup is not None else None,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def report(self, message: str) -> None:
        """Print service status and log it beside the plist's configured checkout."""
        typer.echo(message)
        logs = self.directory.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "server.log").open("a") as output:
            output.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def manage_plist_service(plist: Path, *, upgrade: bool) -> None:
    """Back up, migrate and start the launchd service defined by a plist.

    With upgrade enabled, also pull main and sync dependencies before migration.
    """
    if sys.platform != "darwin":
        raise typer.BadParameter("Server service commands require launchd.")

    # These platform modules must not prevent client-only CLI use on Windows.
    import fcntl
    import pwd

    try:
        with plist.open("rb") as source:
            service = PlistService.model_validate(plistlib.load(source))
    except (OSError, ValueError) as error:
        raise typer.BadParameter(f"Invalid service definition: {error}") from None
    if os.getuid() == 0 or service.user != pwd.getpwuid(os.getuid()).pw_name:
        raise typer.BadParameter(f"Run as service user {service.user!r}, without sudo.")
    if not service.directory.is_absolute():
        raise typer.BadParameter("WorkingDirectory must be absolute.")
    if service.arguments != [
        str(service.directory / ".venv/bin/octomate"),
        "serve",
    ]:
        raise typer.BadParameter(
            "ProgramArguments must run this checkout's octomate serve."
        )
    if not (service.directory / ".venv/bin/python").is_file():
        raise typer.BadParameter(
            "Install the server checkout and its dependencies first."
        )
    if not all(
        service.environment.get(key)
        for key in ("PATH", "OCTOMATE_HOME", "OCTOMATE_DB_URL")
    ):
        raise typer.BadParameter(
            "The service must set PATH, OCTOMATE_HOME and OCTOMATE_DB_URL."
        )
    control = service.directory.parent / "control"
    control.mkdir(parents=True, exist_ok=True)
    with (control / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("Another server operation is running.") from None

        changed_service = False
        try:
            service.maintenance("check")
            loaded = service.loaded()
            if loaded and not upgrade:
                service.report(service.maintenance("verify"))
                service.report("The server is already loaded; no migration was run.")
                return
            if upgrade:
                branch = subprocess.check_output(
                    ["git", "branch", "--show-current"],
                    cwd=service.directory,
                    env=service.process_environment,
                    text=True,
                ).strip()
                dirty = subprocess.check_output(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=service.directory,
                    env=service.process_environment,
                    text=True,
                ).strip()
                if branch != "main" or dirty:
                    raise ValueError(
                        "Upgrade requires main with no tracked local changes."
                    )
                previous = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=service.directory,
                    env=service.process_environment,
                    text=True,
                ).strip()
                service.report(f"Upgrading from {previous}.")

            service.launchctl("disable", service.target)
            changed_service = True
            if loaded:
                service.launchctl("bootout", service.target)
            backup = DatabaseBackup.model_validate_json(service.maintenance("backup"))
            service.report(f"Database: {backup.database}; backup: {backup.backup}.")
            if upgrade:
                subprocess.run(
                    ["git", "pull", "--ff-only", "origin", "main"],
                    cwd=service.directory,
                    env=service.process_environment,
                    check=True,
                )
                revision = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=service.directory,
                    env=service.process_environment,
                    text=True,
                ).strip()
                fetched = subprocess.check_output(
                    ["git", "rev-parse", "FETCH_HEAD"],
                    cwd=service.directory,
                    env=service.process_environment,
                    text=True,
                ).strip()
                if revision != fetched:
                    raise ValueError(
                        "Local main differs from the fetched main; refusing to deploy it."
                    )
                service.report(f"Updating to {revision}.")
                subprocess.run(
                    [
                        "uv",
                        "sync",
                        "--frozen",
                        "--no-dev",
                        "--project",
                        str(service.directory),
                    ],
                    cwd=service.directory,
                    env={
                        **service.process_environment,
                        "UV_PROJECT_ENVIRONMENT": str(service.directory / ".venv"),
                    },
                    check=True,
                )
            service.report(service.maintenance("migrate", backup))
            service.launchctl("enable", service.target)
            service.launchctl("bootstrap", "system", str(plist.resolve()))
            service.report(service.maintenance("verify"))
            service.report(
                "Server started. Check service logs for agent/channel startup."
            )
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            KeyboardInterrupt,
        ) as error:
            if changed_service:
                service.launchctl("disable", service.target)
                if service.loaded():
                    service.launchctl("bootout", service.target)
                service.report(
                    "Operation failed; the server remains disabled until recovery."
                )
            service.report(str(error))
            raise typer.Exit(
                130 if isinstance(error, KeyboardInterrupt) else 1
            ) from error


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
    plist: Annotated[
        Path | None,
        typer.Option(
            "--plist",
            exists=True,
            dir_okay=False,
            help="Back up, migrate and start the launchd service defined by this plist.",
        ),
    ] = None,
) -> None:
    """Run the API; use --tmux to attach or --plist to manage a launchd service.

    Octomate is meant to outlive the terminal that starts it: channels hold their
    sockets open, and the transcript tailers keep watching for native sessions
    started somewhere else entirely. `--tmux` is that shape without a service
    manager — it creates the session if it is missing, and otherwise just attaches
    to the one already serving.
    """
    if plist is not None:
        if (
            host is not None
            or port is not None
            or reload
            or tmux
            or session != "octomate"
        ):
            raise typer.BadParameter(
                "--plist uses the service configuration; do not combine it with "
                "--host, --port, --reload, --tmux or --session."
            )
        manage_plist_service(plist, upgrade=False)
        return

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

    try:
        import uvicorn

        from octomate.config import OctomateConfig  # heavy; only when the CLI serves
    except ImportError as error:
        typer.secho(
            f"`octomate serve` needs the octomate server package ({error.name} is "
            "missing) — octomate-cli alone is the client half.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    if port is not None:
        # The factory reads OctomateConfig() itself; export the override so the
        # config the app is built from — the gateway MCP URL driven runtimes are
        # wired with included — agrees with the bind.
        os.environ["OCTOMATE__PORT"] = str(port)
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


def upgrade(plist: PlistFile = DEFAULT_PLIST) -> None:
    """Upgrade an installed service (launchd/plist only).

    Requires an existing service plist and its server checkout. Run as the plist's
    UserName, without sudo. Foreground servers, tmux sessions and other supervisors
    are not supported by this command.

    Omit --plist to use /Library/LaunchDaemons/io.octomate.server.plist, or run:
    octomate upgrade --plist /absolute/path/to/server.plist

    Pull latest main, sync dependencies, migrate and restart that service.
    """
    manage_plist_service(plist, upgrade=True)
