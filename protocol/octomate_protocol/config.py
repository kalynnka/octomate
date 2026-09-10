"""Deployment config locations shared by the server and operator CLI."""

from __future__ import annotations

import os
from pathlib import Path

OCTOMATE_HOME_ENV = "OCTOMATE_HOME"

# One file per subsystem, in the order they are read. `octomate.yaml` carries the
# host's own settings (host, port, db_url) and comes first so a later
# file cannot be shadowed by it.
CONFIG_FILES: tuple[str, ...] = (
    "octomate.yaml",
    "agents.yaml",
    "channels.yaml",
    "auth.yaml",
    "projects.yaml",
    "providers.yaml",
    "mcp.yaml",
    "observability.yaml",
    "oauth.yaml",
)


def config_home() -> Path:
    """The directory this process reads its deployment from.

    Returned whether or not it exists — a machine with no config at all still names
    a home, which is what `octomate init` writes into and what a boot error can say.
    """
    from_env = os.environ.get(OCTOMATE_HOME_ENV)
    if from_env:
        return Path(from_env).expanduser()
    candidates = (
        Path.cwd() / ".octomate" / "config",
        Path.home() / ".octomate" / "config",
    )
    for candidate in candidates:
        if any((candidate / name).is_file() for name in CONFIG_FILES):
            return candidate
    return candidates[-1]
