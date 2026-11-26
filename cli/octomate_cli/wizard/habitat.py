"""Select a named habitat for each agent before reviewing an installation."""

import re

import typer
from rich.prompt import Prompt

from octomate_cli.credentials import Service
from octomate_cli.habitat import SBX_AGENTS, HabitatMode, parse_agent_habitats
from octomate_cli.installation import DeploymentTarget
from octomate_cli.wizard.base import console

AGENT_SERVICES: dict[str, Service] = {
    "claude": "anthropic",
    "codex": "openai",
    "deepseek": "deepseek",
}


def habitat_step(
    agents: list[str],
    mode: HabitatMode | None,
    image: str | None,
    *,
    assignments: list[str] | None = None,
    interactive: bool,
    target: DeploymentTarget,
) -> tuple[dict[str, HabitatMode], str | None]:
    """Generate one named definition per backend, shared by its selected agents."""
    selections = parse_agent_habitats(agents, assignments or [])
    for agent in agents:
        if agent not in selections:
            choices = [HabitatMode.native, HabitatMode.sbx]
            if agent == "claude":
                choices.insert(1, HabitatMode.srt)
            selections[agent] = mode or (
                HabitatMode(
                    Prompt.ask(
                        f"Habitat for {agent}",
                        choices=[choice.value for choice in choices],
                        default="native",
                        console=console,
                    )
                )
                if interactive
                else HabitatMode.native
            )
        if selections[agent] == HabitatMode.srt and agent != "claude":
            raise typer.BadParameter("SRT is currently supported only for Claude.")
        if selections[agent] == HabitatMode.sbx and agent not in SBX_AGENTS:
            raise typer.BadParameter(
                "sbx supports Claude, Codex, DeepSeek, ACP and Inkling."
            )
    managed = set(selections.values()) - {HabitatMode.native}
    if managed and target == DeploymentTarget.docker:
        raise typer.BadParameter(
            "Managed habitat setup requires a host service; Docker service integration is not supported."
        )
    if HabitatMode.sbx not in managed:
        if image is not None:
            raise typer.BadParameter("--sandbox-image requires an agent selecting sbx.")
        return selections, None
    if image is None and interactive:
        image = Prompt.ask(
            "Sandbox agent image (repository@sha256:digest)", console=console
        )
    if image is None or re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is None:
        raise typer.BadParameter(
            "sbx requires --sandbox-image with an immutable repository@sha256:digest reference."
        )
    return selections, image
