"""Manage the operator CLI independently from the deployed service."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import typer
from rich.console import Console


def upgrade() -> None:
    """Upgrade a standalone uv tool installation of octomate-cli."""
    console = Console(stderr=True, highlight=False, soft_wrap=not sys.stderr.isatty())
    try:
        distribution("octomate")
    except PackageNotFoundError:
        pass
    else:
        console.print(
            "This environment contains the Octomate service. Run the standalone "
            "CLI instead; install it with: uv tool install octomate-cli",
            style="red",
        )
        raise typer.Exit(1)

    installed = distribution("octomate-cli")
    installer = (installed.read_text("INSTALLER") or "unknown").strip()
    prefix = Path(sys.prefix).resolve()
    uv = shutil.which("uv")
    try:
        if installer == "uv" and uv is not None:
            result = subprocess.run(
                [uv, "tool", "dir"], capture_output=True, text=True, check=True
            )
            tool_root = Path(result.stdout.strip()).resolve() / "octomate-cli"
            if (
                prefix == tool_root.resolve()
                and Path(str(installed.locate_file("")))
                .resolve()
                .is_relative_to(prefix)
                and (prefix / "uv-receipt.toml").is_file()
            ):
                executable = prefix / "bin" / "octomate"
                if not executable.is_file():
                    console.print(
                        "The installed CLI executable is missing. Repair it with: "
                        "uv tool install --reinstall octomate-cli",
                        style="red",
                    )
                    raise typer.Exit(1)
                console.print("Upgrading the standalone CLI…", style="cyan")
                subprocess.run([uv, "tool", "upgrade", "octomate-cli"], check=True)
                result = subprocess.run(
                    [str(executable), "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                console.print(result.stdout.strip(), markup=False)
                return

            result = subprocess.run(
                [uv, "cache", "dir"], capture_output=True, text=True, check=True
            )
            if prefix.is_relative_to(Path(result.stdout.strip()).resolve()):
                console.print(
                    "This CLI runs in a temporary uv environment. Refresh the "
                    "invocation with: uvx --refresh --from octomate-cli octomate\n"
                    "For a persistent CLI: uv tool install octomate-cli",
                    style="red",
                )
                raise typer.Exit(1)
    except (OSError, subprocess.CalledProcessError) as error:
        console.print(f"CLI upgrade failed: {error}", style="red", markup=False)
        raise typer.Exit(1) from error

    console.print(
        f"Cannot verify standalone uv tool ownership at {prefix} "
        f"(installer: {installer}).",
        style="red",
        markup=False,
    )
    if installer == "pip":
        console.print(
            "Update this pip installation with: "
            f"{shlex.quote(sys.executable)} -m pip install --upgrade octomate-cli",
            markup=False,
        )
    else:
        console.print(
            "Use this installation's package manager, or install the standalone CLI "
            "with: uv tool install octomate-cli",
        )
    raise typer.Exit(1)
