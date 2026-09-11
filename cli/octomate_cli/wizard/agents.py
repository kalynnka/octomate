from __future__ import annotations

from rich.console import Console

from octomate_cli.wizard.base import TentacleSetup


def claude(console: Console) -> str:
    console.print(
        "Claude uses this desktop account's existing login, settings and plugins. No setup token is requested."
    )
    return "claude"


def codex(console: Console) -> str:
    console.print(
        "Codex uses this desktop account's existing Codex login and configuration."
    )
    return "codex"


def deepseek(console: Console) -> str:
    console.print(
        "DSH is experimental; review agents.deepseek and its harness settings before activation.",
        style="yellow",
    )
    return "deepseek"


AGENTS: dict[str, TentacleSetup[str]] = {
    "claude": TentacleSetup(
        label="Claude Code", capabilities=("Agent",), configure=claude
    ),
    "codex": TentacleSetup(label="Codex", capabilities=("Agent",), configure=codex),
    "deepseek": TentacleSetup(
        label="DSH (experimental)", capabilities=("Agent",), configure=deepseek
    ),
}
