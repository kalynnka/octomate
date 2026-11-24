"""The agent tentacles the init wizard offers, and what each says when selected."""

from __future__ import annotations

import shlex

import typer
from rich.console import Console
from rich.prompt import Prompt

from octomate_cli.wizard.base import TentacleSetup


def claude(console: Console) -> str:
    console.print(
        "Claude uses the service account's login. Docker requires a separate container login or explicit credentials. Driven sessions disable local customizations; configure tools in Octomate. No credentials are collected here."
    )
    return "claude"


def codex(console: Console) -> str:
    console.print(
        "Codex uses the service account's login. Docker requires a separate container login. Driven sessions disable local plugins, hooks, apps and MCPs; configure tools in Octomate."
    )
    return "codex"


def deepseek(console: Console) -> str:
    console.print(
        "DSH is experimental. Octomate owns a separate runtime and shares native settings and sessions; local plugins, hooks and MCPs are disabled. Review tentacles.deepseek before activation.",
        style="yellow",
    )
    return "deepseek"


def acp(console: Console) -> str:
    console.print(
        "ACP connects an installed provider over stdio. Install it in the service "
        "environment and, for sbx, in the sandbox image too. Configure the provider's "
        "authentication separately."
    )
    return "acp"


def acp_step(
    selected: bool,
    command: str | None,
    arguments: list[str] | None,
    *,
    interactive: bool,
    console: Console,
) -> tuple[str | None, list[str]]:
    if not selected:
        if command is not None or arguments is not None:
            raise typer.BadParameter("--acp-command and --acp-arg require --agent acp.")
        return None, []
    if command is None and interactive:
        command = Prompt.ask("ACP provider executable", console=console)
    if command is None or not command.strip():
        raise typer.BadParameter("ACP requires a non-empty --acp-command.")
    if arguments is None and interactive:
        text = Prompt.ask(
            "ACP arguments (quote arguments containing spaces)",
            default="",
            console=console,
        )
        try:
            arguments = shlex.split(text)
        except ValueError as error:
            raise typer.BadParameter(f"Invalid ACP arguments: {error}") from error
    return command.strip(), arguments or []


AGENTS: dict[str, TentacleSetup[str]] = {
    "claude": TentacleSetup(
        label="Claude Code", capabilities=("Agent",), configure=claude
    ),
    "codex": TentacleSetup(label="Codex", capabilities=("Agent",), configure=codex),
    "deepseek": TentacleSetup(
        label="DSH (experimental)", capabilities=("Agent",), configure=deepseek
    ),
    "acp": TentacleSetup(label="ACP provider", capabilities=("Agent",), configure=acp),
}
