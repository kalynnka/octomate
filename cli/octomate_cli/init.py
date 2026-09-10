"""Interactive service setup: installation, network, agents, channels and preparation."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import TypeAdapter
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from octomate_cli.mcp import McpPreset
from octomate_cli.wizard.agents import AGENTS, agents_step
from octomate_cli.wizard.base import brand_color, console
from octomate_cli.wizard.channels import CHANNELS, channels_step
from octomate_cli.wizard.mcp import MCPS, mcps_step

if TYPE_CHECKING:
    from octomate_cli.service import PlistService


def init(
    prepare: Annotated[
        bool,
        typer.Option(
            "--prepare",
            help="Prepare files only; do not initialize data or start services.",
        ),
    ] = False,
    root: Annotated[
        Path | None,
        typer.Option(help="Service installation directory; prompts when omitted."),
    ] = None,
    source: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Copy a local Git working tree, including uncommitted files, for development.",
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(min=1, max=65535, help="Loopback port for the service."),
    ] = None,
    agent: Annotated[
        list[str] | None,
        typer.Option(
            "--agent",
            help="Agent template: claude, codex or deepseek (DSH; experimental). Repeat for multiple agents.",
        ),
    ] = None,
    channel: Annotated[
        list[str] | None,
        typer.Option(
            "--channel",
            help="Channel template: slack, lark, discord, trunkline or none. Repeat for multiple channels; fill credentials in config later.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm preparation with explicit root, port, agent and channel choices; skip optional MCP setup.",
        ),
    ] = False,
) -> None:
    """Prepare a new macOS service through an interactive setup wizard.

    Currently requires --prepare. Creates a private installation and a reviewable
    LaunchAgent definition inside it. Database initialization, account creation,
    service activation and live Claude verification are separate, unfinished steps.
    """
    if not prepare:
        raise typer.BadParameter(
            "Use --prepare; service activation is not implemented yet."
        )
    if sys.platform != "darwin":
        raise typer.BadParameter("The deployment wizard currently supports macOS.")
    # pwd is unavailable on Windows; client CLI imports must remain portable.
    import pwd

    if os.getuid() == 0:
        raise typer.BadParameter("Run as the desktop account, without sudo.")
    account = pwd.getpwuid(os.getuid())
    console.print(
        Panel(
            "Generate config templates for an isolated service installation.\n"
            "Preparation does not start services or write databases.",
            title="Octomate init · prepare",
            border_style=brand_color,
        )
    )
    root = installation_step(root, account.pw_dir, yes)
    if (root / "control/io.octomate.server.plist").exists():
        if (
            source is not None
            or port is not None
            or agent is not None
            or channel is not None
        ):
            raise typer.BadParameter(
                "Prepared installations retain their settings. Omit --source, --port, --agent and --channel to validate again."
            )
        validate_existing(root, account.pw_dir)
        return
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise typer.BadParameter(
            "Choose an empty installation directory. Existing files will not be overwritten."
        )
    git = shutil.which("git")
    uv = shutil.which("uv")
    if git is None or uv is None:
        raise typer.BadParameter("Install Git and uv before preparing a service.")
    source, revision = select_source(root, source, git)
    if yes and (port is None or agent is None or channel is None):
        raise typer.BadParameter(
            "--yes requires --port, --agent and --channel (use none for no channels)."
        )
    port = network_step(port)
    agent = agents_step(agent, console=console)
    channels = channels_step(channel, console=console)
    mcps = mcps_step(interactive=not yes, console=console)
    service = build_service(root, account.pw_dir, account.pw_name)
    review_step(service, source, revision, port, agent, channels, mcps, yes)
    prepare_step(service, source, revision, git, uv, port, agent, channels, mcps)


def installation_step(root: Path | None, home: str, yes: bool) -> Path:
    console.print("1/7 · Installation", style=f"bold {brand_color}")
    if root is None:
        if yes:
            raise typer.BadParameter("--yes requires --root.")
        root = Path(
            Prompt.ask(
                "Installation directory",
                default=str(Path(home) / "Library/Application Support/Octomate"),
                console=console,
            )
        )
    root = root.expanduser()
    if root.is_symlink():
        raise typer.BadParameter("The installation directory must not be a symlink.")
    root = root.resolve()
    return root


def validate_existing(root: Path, home: str) -> None:
    # Imported here because service owns this model and registers the init command.
    from octomate_cli.service import PlistService

    draft = root / "control/io.octomate.server.plist"
    try:
        if draft.is_symlink():
            raise ValueError("The prepared definition must not be a symlink.")
        service = PlistService.model_validate(plistlib.loads(draft.read_bytes()))
        if (
            service.directory != root
            or service.arguments
            != [str(root / "app/.venv/bin/octomate"), "service", "serve"]
            or service.environment.get("OCTOMATE_HOME") != str(root / "config")
            or service.environment.get("OCTOMATE_DB_URL")
            != f"sqlite+aiosqlite:///{root / 'octomate.db'}"
            or service.environment.get("HOME") != home
        ):
            raise ValueError(
                "The prepared definition does not match this installation and account."
            )
        service.maintenance("check")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise typer.BadParameter(
            f"Prepared configuration validation failed: {error}"
        ) from None
    console.print(
        "Configuration structure is valid; files and credentials are unchanged.",
        style="green",
    )
    console.print(
        "Follow CONFIGURATION.md to complete configuration. Service remains uninstalled; live authentication, connectors and plugins have not been verified."
    )
    return


def select_source(root: Path, source: Path | None, git: str) -> tuple[Path | None, str]:
    # Deferred to avoid a cycle with the service command registration.
    from octomate_cli.service import latest_server_release

    if source is not None:
        source = source.resolve()
        if root.is_relative_to(source) or source.is_relative_to(root):
            raise typer.BadParameter(
                "The installation and source checkout must be separate directories."
            )
        try:
            source_root = Path(
                subprocess.check_output(
                    [git, "-C", str(source), "rev-parse", "--show-toplevel"], text=True
                ).strip()
            ).resolve()
        except subprocess.CalledProcessError:
            raise typer.BadParameter("--source must point to a Git checkout.") from None
        if source_root != source:
            raise typer.BadParameter(
                "--source must point to the root of the Git checkout."
            )
        revision = "Working tree snapshot (no release tag)"
        console.print(
            f"Source: {source} (selected by --source). No release tag is selected."
        )
    else:
        with console.status("Finding the latest stable service release…"):
            try:
                revision = latest_server_release().tag_name
            except (OSError, ValueError) as error:
                raise typer.BadParameter(f"Release lookup failed: {error}") from None
        console.print(
            f"Selected {revision}: highest stable octomate-vX.Y.Z GitHub release."
        )
    return source, revision


def network_step(port: int | None) -> int:
    console.print("2/7 · Network", style=f"bold {brand_color}")
    while port is None:
        value = IntPrompt.ask("Loopback port", default=8000, console=console)
        if 1 <= value <= 65535:
            port = value
        else:
            console.print("Choose a port from 1 to 65535.", style="yellow")
    return port


def build_service(root: Path, home: str, username: str) -> PlistService:
    # Imported here because service owns this model and registers the init command.
    from octomate_cli.service import PlistService

    environment = {
        "HOME": home,
        "USER": username,
        "LOGNAME": username,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OCTOMATE_HOME": str(root / "config"),
        "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{root / 'octomate.db'}",
    }
    for key in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if key in environment:
            environment[key] = str(Path(environment[key]).expanduser().resolve())
    return PlistService(
        Label="io.octomate.server",
        WorkingDirectory=root,
        ProgramArguments=[str(root / "app/.venv/bin/octomate"), "service", "serve"],
        EnvironmentVariables=environment,
        StandardOutPath=root / "logs/stdout.log",
        StandardErrorPath=root / "logs/stderr.log",
        LimitLoadToSessionType="Aqua",
        KeepAlive=True,
    )


def review_step(
    service: PlistService,
    source: Path | None,
    revision: str,
    port: int,
    agent: list[str],
    channels: list[str],
    mcps: list[McpPreset],
    yes: bool,
) -> None:
    root = service.directory
    environment = service.environment
    draft = root / "control/io.octomate.server.plist"
    console.print("6/7 · Review", style=f"bold {brand_color}")
    table = Table(box=None, header_style="bold")
    table.add_column("Setting", no_wrap=True, style="dim")
    table.add_column("Value", overflow="fold")
    for key, value in (
        ("Installation", str(root)),
        ("Version", revision),
        ("Source", str(source) if source else "github.com/kalynnka/octomate"),
        ("Bind", f"127.0.0.1:{port}"),
        (
            "Agents Tentacles",
            ", ".join(AGENTS[name].label for name in agent),
        ),
        (
            "Channels Tentacles",
            ", ".join(CHANNELS[name].label for name in channels) or "None",
        ),
        (
            "MCP Tentacles",
            ", ".join(
                f"{MCPS[mcp.provider].label} ({mcp.name}, {'read-only' if mcp.read_only else 'read/write'})"
                for mcp in mcps
            )
            or "None",
        ),
        (
            "Claude home",
            environment.get(
                "CLAUDE_CONFIG_DIR", str(Path(environment["HOME"]) / ".claude")
            )
            if "claude" in agent
            else "Not enabled",
        ),
        (
            "Codex home",
            environment.get("CODEX_HOME", str(Path(environment["HOME"]) / ".codex"))
            if "codex" in agent
            else "Not enabled",
        ),
        ("Launch context", f"{environment['USER']} · {service.domain}"),
        ("Configuration", str(root / "config")),
        ("Auth secrets", str(root / ".env")),
        ("Configuration checklist", str(root / "CONFIGURATION.md")),
        ("Database (not created)", str(root / "octomate.db")),
        ("Draft LaunchAgent", str(draft)),
    ):
        table.add_row(key, value)
    console.print(table)
    console.print("The GUI service will require a desktop login; logging out stops it.")
    if not yes and not Confirm.ask(
        "Prepare these files?", default=True, console=console
    ):
        raise typer.Abort()


def prepare_step(
    service: PlistService,
    source: Path | None,
    revision: str,
    git: str,
    uv: str,
    port: int,
    agent: list[str],
    channels: list[str],
    mcps: list[McpPreset],
) -> None:
    root = service.directory
    environment = service.environment
    draft = root / "control/io.octomate.server.plist"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for directory in ("control", "logs", "backups"):
        (root / directory).mkdir(mode=0o700)
    log_path = root / "logs/prepare.log"
    console.print("7/7 · Prepare and validate", style=f"bold {brand_color}")
    console.print(f"Build log: {log_path}", style="dim")
    checkout = root / "app"
    build_env = {**environment, "UV_PROJECT_ENVIRONMENT": str(checkout / ".venv")}
    if "UV_CACHE_DIR" in os.environ:
        build_env["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    try:
        with log_path.open("x") as log:
            log_path.chmod(0o600)
            with console.status(
                "Installing the selected service and locked dependencies…"
            ):
                subprocess.run(
                    [git, "clone", "--no-hardlinks", str(source), str(checkout)]
                    if source
                    else [
                        git,
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        revision,
                        "https://github.com/kalynnka/octomate.git",
                        str(checkout),
                    ],
                    env=build_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
                if source is not None:
                    copy_working_tree(source, checkout, git)
                subprocess.run(
                    [uv, "sync", "--locked", "--no-dev", "--project", str(checkout)],
                    cwd=root,
                    env=build_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            with console.status("Generating private configuration and validating it…"):
                subprocess.run(
                    [
                        str(checkout / ".venv/bin/python"),
                        "-m",
                        "octomate_cli.deployment",
                        "prepare",
                        "--port",
                        str(port),
                        *(argument for name in agent for argument in ("--agent", name)),
                        *(
                            argument
                            for name in channels
                            for argument in ("--channel", name)
                        ),
                        *(["--mcp-presets"] if mcps else []),
                    ],
                    input=TypeAdapter(list[McpPreset]).dump_json(mcps)
                    if mcps
                    else None,
                    cwd=root,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
                service.maintenance("check")
        payload = service.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload.update(RunAtLoad=True, ThrottleInterval=10, Umask=63)
        with draft.open("xb") as output:
            draft.chmod(0o600)
            plistlib.dump(payload, output)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        console.print(f"Preparation failed: {error}", style="red")
        console.print(
            f"Inspect {log_path}. No service was activated; this directory may contain incomplete preparation files."
        )
        raise typer.Exit(1) from error
    console.print(
        Panel(
            Text.assemble(
                ("Installation\n", "bold"),
                str(root),
                ("\n\nNext steps\n", f"bold {brand_color}"),
                "Paths below are relative to the installation.\n",
                "1. Complete ",
                ("CONFIGURATION.md", "bold"),
                " to finish your configuration.\n",
                "2. Review ",
                ("control/io.octomate.server.plist", "bold"),
                ".\n",
                ("\nStill pending\n", "bold"),
                "Database and account creation; service installation and startup.\n",
                "Live agent, connector and plugin verification.",
            ),
            title="Preparation complete",
            border_style="green",
        )
    )


def copy_working_tree(source: Path, checkout: Path, git: str) -> None:
    """Snapshot Git-visible working files without sharing ignored local config or data."""
    files = (
        subprocess.check_output(
            [
                git,
                "-C",
                str(source),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ]
        )
        .decode()
        .split("\0")
    )
    deleted = (
        subprocess.check_output(
            [
                git,
                "-C",
                str(source),
                "diff",
                "--name-only",
                "--diff-filter=D",
                "HEAD",
                "-z",
            ]
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, deleted):
        destination = checkout / name
        if not destination.parent.resolve().is_relative_to(checkout):
            raise ValueError(f"Destination must stay inside the checkout: {name}")
        destination.unlink(missing_ok=True)
    for name in filter(None, files):
        origin = source / name
        if origin.is_symlink() or not origin.resolve().is_relative_to(source):
            raise ValueError(f"Source file must stay inside the checkout: {name}")
        if not origin.exists():
            continue
        destination = checkout / name
        if destination.is_symlink() or not destination.resolve().is_relative_to(
            checkout
        ):
            raise ValueError(f"Destination must stay inside the checkout: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
