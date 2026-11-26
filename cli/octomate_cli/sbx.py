"""The sbx executable contract shared by independent operator commands."""

import os
import re
import shutil
import subprocess
import sys

import typer


def run_sbx(executable: str, *arguments: str, capture: bool = False) -> str:
    """Keep login prompts and errors attached to the operator's terminal."""
    try:
        result = subprocess.run(
            [executable, "--debug=false", *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )
    except subprocess.CalledProcessError as error:
        raise typer.BadParameter(
            f"sbx {' '.join(arguments)} failed with exit code {error.returncode}; "
            "see its error above."
        ) from error
    return result.stdout or ""


def executable() -> str:
    """Require the supported sbx version under the service account."""
    if sys.platform not in {"darwin", "linux"} or os.getuid() == 0:
        raise typer.BadParameter(
            "Run on macOS or Linux as the service account, without sudo."
        )
    executable = shutil.which("sbx")
    if executable is None:
        raise typer.BadParameter(
            "Install sbx 0.48.0, then rerun this command: https://docs.docker.com/ai/sandboxes/install/"
        )
    version = run_sbx(executable, "version", capture=True)
    if re.search(r"\bv0\.48\.0(?:\s|$)", version) is None:
        raise typer.BadParameter("This Octomate version requires sbx 0.48.0.")
    return executable
