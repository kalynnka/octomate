"""The agent tentacles the init wizard offers, and what each says when selected."""

from __future__ import annotations

from rich.console import Console

from octomate_cli.wizard.base import TentacleSetup


def claude(console: Console) -> str:
    console.print(
        "Claude uses this desktop account's existing login. Driven sessions disable local customizations; configure tools in Octomate. No setup token is requested."
    )
    return "claude"


def codex(console: Console) -> str:
    console.print(
        "Codex uses this desktop account's existing login. Driven sessions disable local plugins, hooks, apps and MCPs; configure tools in Octomate."
    )
    return "codex"


def deepseek(console: Console) -> str:
    console.print(
        "DSH is experimental. Octomate owns a separate runtime and shares native settings and sessions; local plugins, hooks and MCPs are disabled. Review tentacles.deepseek before activation.",
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
