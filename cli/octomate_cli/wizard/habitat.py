"""Select a named habitat for each agent before reviewing an installation."""

import re

import typer
from rich.prompt import Prompt

from octomate_cli.credentials import Service
from octomate_cli.habitat import HabitatProvider, parse_agent_habitats
from octomate_cli.installation import DeploymentTarget
from octomate_cli.wizard.agents import AGENTS
from octomate_cli.wizard.base import console

AGENT_SERVICES: dict[str, Service] = {
    "claude": "anthropic",
    "codex": "openai",
    "deepseek": "deepseek",
}


def habitat_step(
    agents: list[str],
    provider: HabitatProvider | None,
    image: str | None,
    *,
    assignments: list[str] | None = None,
    interactive: bool,
    target: DeploymentTarget,
) -> tuple[dict[str, HabitatProvider], str | None]:
    """Generate one named definition per backend, shared by its selected agents."""
    selections = parse_agent_habitats(agents, assignments or [])
    for agent in agents:
        if agent not in AGENTS:
            raise typer.BadParameter(f"Agent {agent!r} has no supported habitat setup.")
        if agent not in selections:
            choices = [
                choice
                for choice in HabitatProvider
                if choice.type in AGENTS[agent].habitat_types
            ]
            selections[agent] = provider or (
                HabitatProvider(
                    Prompt.ask(
                        f"Habitat for {agent}",
                        choices=[choice.value for choice in choices],
                        default="native",
                        console=console,
                    )
                )
                if interactive
                else HabitatProvider.native
            )
        if selections[agent].type not in AGENTS[agent].habitat_types:
            raise typer.BadParameter(
                f"Agent {agent!r} does not support {selections[agent].type} habitats."
            )
    managed = set(selections.values()) - {HabitatProvider.native}
    if managed and target == DeploymentTarget.docker:
        raise typer.BadParameter(
            "Managed habitat setup requires a host service; Docker service integration is not supported."
        )
    if HabitatProvider.sbx not in managed:
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
