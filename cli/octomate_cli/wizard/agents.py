from __future__ import annotations

from rich.console import Console

from octomate_cli.wizard.base import TentacleSetup, brand_color, select_tentacles


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
    "claude": TentacleSetup(label="Claude Code", configure=claude, checked=True),
    "codex": TentacleSetup(label="Codex", configure=codex),
    "deepseek": TentacleSetup(label="DSH (experimental)", configure=deepseek),
}


def agents_step(agent: list[str] | None, *, console: Console) -> list[str]:
    console.print("3/7 · Agents Tentacles", style=f"bold {brand_color}")
    return select_tentacles(
        AGENTS,
        agent,
        console=console,
        prompt="Select agent tentacles",
        invalid_message="Choose at least one supported --agent: claude, codex, deepseek (DSH; experimental).",
        required=True,
    )
