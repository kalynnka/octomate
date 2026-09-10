"""Run the server and manage the desktop account's GUI service."""

from __future__ import annotations

import asyncio
import inspect
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import typer
from octomate_protocol.deployment import DatabaseBackup
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter
from rich.console import Console
from rich.table import Table

from octomate_cli.init import init
from octomate_cli.users import user_typer

RELEASE_URL = "https://api.github.com/repos/kalynnka/octomate/releases"
console = Console(stderr=True, markup=False, highlight=False)
service_typer = typer.Typer(help="Run the server and manage its macOS GUI service.")
service_typer.command()(init)
service_typer.add_typer(user_typer, name="user")


class Release(BaseModel):
    """GitHub release metadata used to select a server upgrade."""

    tag_name: str
    draft: bool
    prerelease: bool


def latest_server_release() -> Release:
    """Find the highest stable server version across all release pages."""
    latest: Release | None = None
    latest_version = (0, 0, 0)
    page = 1
    adapter = TypeAdapter(list[Release])
    while True:
        request = Request(
            f"{RELEASE_URL}?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urlopen(request, timeout=30) as response:
            releases = adapter.validate_json(response.read())
        for release in releases:
            match = re.fullmatch(
                r"octomate-v([0-9]+)\.([0-9]+)\.([0-9]+)", release.tag_name
            )
            if release.draft or release.prerelease or match is None:
                continue
            version = tuple(int(part) for part in match.groups())
            if latest is None or version > latest_version:
                latest, latest_version = release, version
        if len(releases) < 100:
            break
        page += 1
    if latest is None:
        raise ValueError("No stable octomate-vX.Y.Z server release is available.")
    return latest


class PlistService(BaseModel):
    """A launchd job and its runtime configuration, loaded from a service plist."""

    model_config = ConfigDict(hide_input_in_errors=True)

    label: str = Field(alias="Label", pattern=r"^[a-zA-Z0-9_.-]+$")
    directory: Path = Field(alias="WorkingDirectory")
    user: None = Field(default=None, alias="UserName")
    group: None = Field(default=None, alias="GroupName")
    arguments: list[str] = Field(alias="ProgramArguments")
    environment: dict[str, str] = Field(alias="EnvironmentVariables", repr=False)
    stdout: Path = Field(alias="StandardOutPath")
    stderr: Path = Field(alias="StandardErrorPath")
    session_type: Literal["Aqua"] = Field(alias="LimitLoadToSessionType")
    program: None = Field(default=None, alias="Program")
    keep_alive: Literal[True] = Field(alias="KeepAlive")
    abandon_process_group: Literal[False] = Field(
        default=False, alias="AbandonProcessGroup"
    )

    @staticmethod
    def path() -> Path:
        return Path.home() / "Library/LaunchAgents/io.octomate.server.plist"

    @classmethod
    def load(cls) -> PlistService:
        if sys.platform != "darwin":
            raise typer.BadParameter("Service commands require macOS launchd.")
        # pwd is unavailable on Windows; client CLI imports must remain portable.
        import pwd

        if os.getuid() == 0:
            raise typer.BadParameter("Run as the desktop account, without sudo.")
        path = cls.path()
        try:
            if path.stat().st_uid != os.getuid():
                raise ValueError("The service definition must belong to this account.")
            with path.open("rb") as source:
                service = cls.model_validate(plistlib.load(source))
        except (OSError, ValueError) as error:
            raise typer.BadParameter(
                f"Invalid GUI service definition: {error}"
            ) from None
        account = pwd.getpwuid(os.getuid())
        if service.label != "io.octomate.server":
            raise typer.BadParameter("Unexpected service label.")
        if (
            len(service.arguments) != 3
            or service.arguments[1:] != ["service", "serve"]
            or not Path(service.arguments[0]).is_absolute()
            or Path(service.arguments[0]).parts[-3:] != (".venv", "bin", "octomate")
        ):
            raise typer.BadParameter(
                "The service must run <checkout>/.venv/bin/octomate service serve."
            )
        if not all(
            path.is_absolute()
            for path in (service.directory, service.stdout, service.stderr)
        ):
            raise typer.BadParameter(
                "Working directory and log paths must be absolute."
            )
        if not all(
            service.environment.get(key)
            for key in ("PATH", "OCTOMATE_HOME", "OCTOMATE_DB_URL")
        ):
            raise typer.BadParameter(
                "The service must set PATH, OCTOMATE_HOME and OCTOMATE_DB_URL."
            )
        if service.environment.get("HOME") != account.pw_dir or any(
            service.environment.get(key) != account.pw_name
            for key in ("USER", "LOGNAME")
        ):
            raise typer.BadParameter(
                "The service must use this desktop account's HOME, USER and LOGNAME."
            )
        return service

    @property
    def checkout(self) -> Path:
        return Path(self.arguments[0]).parents[2]

    @property
    def root(self) -> Path:
        return self.checkout.parent

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def target(self) -> str:
        """The desktop launchd job identified by the plist's Label."""
        return f"{self.domain}/{self.label}"

    def require_gui(self) -> None:
        result = subprocess.run(
            ["/bin/launchctl", "print", self.domain], capture_output=True, text=True
        )
        if result.returncode:
            raise ValueError(
                f"Desktop session {self.domain} is unavailable. Log into the desktop first."
            )

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
        """Apply an unprivileged action in the desktop launchd domain."""
        subprocess.run(["/bin/launchctl", action, *arguments], check=True)

    def stop(self) -> None:
        """Disable the job and wait for its process group to exit."""
        self.launchctl("disable", self.target)
        if not self.loaded():
            return
        result = subprocess.run(
            ["/bin/launchctl", "print", self.target],
            capture_output=True,
            text=True,
            check=True,
        )
        match = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
        group: int | None = None
        if match is not None:
            try:
                group = os.getpgid(int(match[1]))
            except ProcessLookupError:
                pass
        self.launchctl("bootout", self.target)
        deadline = time.monotonic() + 30
        while group is not None:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "The service process group did not stop within 30 seconds."
                )
            time.sleep(0.25)

    def maintenance(self, action: str, backup: DatabaseBackup | None = None) -> str:
        """Run maintenance with the plist's working directory and environment."""
        result = subprocess.run(
            [
                str(self.checkout / ".venv/bin/python"),
                "-m",
                "octomate_cli.deployment",
                action,
            ],
            cwd=self.directory,
            env={
                **self.environment,
                "OCTOMATE_DEPLOYMENT_ROOT": str(self.root),
            },
            input=backup.model_dump_json() if backup is not None else None,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def report(self, message: str) -> None:
        """Print service status and log it beside the plist's configured checkout."""
        console.print(message)
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "server.log").open("a") as output:
            output.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def manage_service(action: Literal["start", "stop", "restart", "upgrade"]) -> None:
    """Serialize service changes, preserving the installed release on ordinary starts."""
    service = PlistService.load()
    if (
        action == "upgrade"
        and Path(sys.prefix).resolve() == (service.checkout / ".venv").resolve()
    ):
        raise typer.BadParameter(
            "Run service upgrade from the standalone CLI, not the service environment. "
            "Install it with: uv tool install octomate-cli"
        )
    # fcntl is unavailable on Windows; client CLI imports must remain portable.
    import fcntl

    upgrade = action == "upgrade"
    control = service.root / "control"
    control.mkdir(parents=True, exist_ok=True)
    with (control / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("Another server operation is running.") from None

        changed_service = False
        try:
            service.require_gui()
            if action == "stop":
                service.stop()
                service.report("Service stopped and disabled.")
                return
            service.maintenance("check" if upgrade else "ready")
            loaded = service.loaded()
            if loaded and action == "start":
                service.report(service.maintenance("verify"))
                service.report("The server is already loaded; no migration was run.")
                return
            if upgrade:
                dirty = subprocess.check_output(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=service.checkout,
                    env=service.environment,
                    text=True,
                ).strip()
                if dirty:
                    raise ValueError("Upgrade requires no tracked local changes.")
                previous = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=service.checkout,
                    env=service.environment,
                    text=True,
                ).strip()
                release = latest_server_release()
                subprocess.run(
                    ["git", "fetch", "origin", f"refs/tags/{release.tag_name}"],
                    cwd=service.checkout,
                    env=service.environment,
                    check=True,
                )
                revision = subprocess.check_output(
                    ["git", "rev-parse", "FETCH_HEAD^{commit}"],
                    cwd=service.checkout,
                    env=service.environment,
                    text=True,
                ).strip()
                if previous == revision:
                    service.report(f"Already at {release.tag_name}; no update needed.")
                    return
                ancestor = subprocess.check_output(
                    ["git", "merge-base", previous, revision],
                    cwd=service.checkout,
                    env=service.environment,
                    text=True,
                ).strip()
                if ancestor != previous:
                    raise ValueError(
                        "The latest release does not contain the installed commit; "
                        "refusing a downgrade or divergent history."
                    )
                service.report(
                    f"Upgrading from {previous} to {release.tag_name} ({revision})."
                )

            changed_service = True
            service.stop()
            if upgrade:
                backup = DatabaseBackup.model_validate_json(
                    service.maintenance("backup")
                )
                service.report(f"Database: {backup.database}; backup: {backup.backup}.")
                subprocess.run(
                    ["git", "checkout", "--detach", revision],
                    cwd=service.checkout,
                    env=service.environment,
                    check=True,
                )
                subprocess.run(
                    [
                        "uv",
                        "sync",
                        "--locked",
                        "--no-dev",
                        "--project",
                        str(service.checkout),
                    ],
                    cwd=service.checkout,
                    env={
                        **service.environment,
                        "UV_PROJECT_ENVIRONMENT": str(service.checkout / ".venv"),
                    },
                    check=True,
                )
                service.report(service.maintenance("migrate", backup))
            else:
                service.maintenance("stopped")
            service.launchctl("enable", service.target)
            service.launchctl("bootstrap", service.domain, str(service.path()))
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


@service_typer.command()
def upgrade() -> None:
    """Update the GUI service's release, dependencies and database, then restart."""
    manage_service("upgrade")


@service_typer.command()
def start() -> None:
    """Ask macOS launchd to start and manage the installed GUI service.

    Does not update code or migrate data. Use `service serve` to run the server
    directly in the foreground for Docker or development.
    """
    manage_service("start")


@service_typer.command()
def stop() -> None:
    """Stop the GUI service and keep it disabled across desktop logins."""
    manage_service("stop")


@service_typer.command()
def restart() -> None:
    """Restart the installed GUI service without updating code or migrating data."""
    manage_service("restart")


@service_typer.command()
def verify() -> None:
    """Check schema and protected HTTP routes; no model or connector requests."""
    service = PlistService.load()
    try:
        service.require_gui()
        if not service.loaded():
            raise ValueError("The service is not loaded. Run octomate service start.")
        service.maintenance("ready")
        console.print(service.maintenance("verify"))
        console.print("Not checked: Claude login, connectors and plugins.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        console.print(str(error), style="red")
        raise typer.Exit(1) from error


@service_typer.command()
def status() -> None:
    """Show the installed GUI job and its configured paths without modifying it."""
    service = PlistService.load()
    try:
        service.require_gui()
        result = subprocess.run(
            ["/bin/launchctl", "print", service.target], capture_output=True, text=True
        )
        if result.returncode != 113:
            result.check_returncode()
        pid = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
        disabled = subprocess.check_output(
            ["/bin/launchctl", "print-disabled", service.domain], text=True
        )
        explicitly_disabled = (
            re.search(rf'"{re.escape(service.label)}"\s*=>\s*true', disabled)
            is not None
        )
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=service.checkout,
            env=service.environment,
            text=True,
        ).strip()
        table = Table("Service", "Value", box=None)
        for name, value in (
            ("Target", service.target),
            ("Enabled", "No" if explicitly_disabled else "Yes"),
            ("Loaded", "Yes" if result.returncode == 0 else "No"),
            ("PID", pid[1] if pid else "Not running"),
            ("Revision", revision),
            ("Directory", str(service.directory)),
            ("Checkout", str(service.checkout)),
            ("Config", service.environment["OCTOMATE_HOME"]),
            ("Stdout", str(service.stdout)),
            ("Stderr", str(service.stderr)),
        ):
            table.add_row(name, value)
        console.print(table)
        console.print("Requires a desktop login; logout stops the service.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        console.print(str(error), style="red")
        raise typer.Exit(1) from error


@service_typer.command()
def logs(
    follow: Annotated[
        bool, typer.Option(help="Follow new service log output.")
    ] = False,
) -> None:
    """Read the configured stdout and stderr logs."""
    service = PlistService.load()
    paths = list(dict.fromkeys((str(service.stdout), str(service.stderr))))
    try:
        subprocess.run(
            ["/usr/bin/tail", "-n", "100", *(["-f"] if follow else []), "--", *paths],
            check=True,
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (OSError, subprocess.CalledProcessError) as error:
        console.print(str(error), style="red")
        raise typer.Exit(1) from error


@service_typer.command()
def invite(
    url: Annotated[
        str | None,
        typer.Option(help="Print a registration link for this URL instead of a code."),
    ] = None,
) -> None:
    """Issue an anonymous, single-use registration invitation."""
    if find_spec("octomate") is None:
        raise typer.BadParameter("Invitations require the Octomate server package")

    # Server imports stay here so the standalone client CLI remains usable.
    from octomate.config import OctomateConfig
    from octomate.managers.auth import AuthManager
    from octomate.schemas.base import sqlalchemy_materia

    base_url = TypeAdapter(HttpUrl).validate_python(url) if url is not None else None
    config = OctomateConfig()
    if config.auth is None:
        raise typer.BadParameter("Configure auth.yaml before issuing invitations")
    manager = AuthManager(config.auth)

    async def issue() -> str:
        with sqlalchemy_materia():
            token = await manager.invite()
        return token.get_secret_value()

    token = asyncio.run(issue())
    if base_url is None:
        typer.echo(token)
    else:
        fragment = urlencode({"invitation": token})
        typer.echo(f"{str(base_url).rstrip('/')}/#{fragment}")


@service_typer.command()
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
    """Run the server directly in the foreground for Docker or development.

    Use `service start` to have macOS launchd start and manage the installed GUI
    service. Use --tmux to create or attach to a persistent terminal session.
    """
    if find_spec("octomate") is None:
        raise typer.BadParameter("Serving requires the Octomate server package")

    # Server imports stay here so the standalone client CLI remains usable.
    import uvicorn

    from octomate.config import OctomateConfig

    if tmux:
        if shutil.which("tmux") is None:
            typer.secho("tmux is not installed.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        serving = subprocess.run(
            ["tmux", "has-session", "-t", session], capture_output=True
        )
        if serving.returncode != 0:
            command = [
                # Keep the active virtualenv when tmux starts outside this shell.
                sys.executable,
                "-m",
                "octomate_cli.main",
                "service",
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

    if port is not None:
        # The factory reads OctomateConfig() itself; export the override so the
        # config the app is built from — the Octomate MCP URL driven runtimes are
        # wired with included — agrees with the bind.
        os.environ["OCTOMATE__PORT"] = str(port)
    config = OctomateConfig()
    uvicorn.run(
        "octomate.app:create_app",
        factory=True,
        host=str(config.host) if host is None else host,
        port=config.port if port is None else port,
        reload=reload,
        # Watch application code without watching the deployment's mutable data.
        reload_dirs=[str(Path(inspect.getfile(OctomateConfig)).parent.parent)],
        log_level=config.logging.level.lower(),
    )
