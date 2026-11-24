"""Select the service-wide habitat before reviewing an installation."""

import re

import typer
from rich.prompt import Prompt

from octomate_cli.habitat import AGENT_SERVICES, HabitatMode
from octomate_cli.installation import DeploymentTarget
from octomate_cli.wizard.base import console


def habitat_step(
    agents: list[str],
    mode: HabitatMode | None,
    image: str | None,
    *,
    interactive: bool,
    target: DeploymentTarget,
) -> str | None:
    """Return the selected sbx image, or None for native execution."""
    if mode is None:
        mode = (
            HabitatMode(
                Prompt.ask(
                    "Habitat for all selected agents",
                    choices=[choice.value for choice in HabitatMode],
                    default="native",
                    console=console,
                )
            )
            if interactive
            else HabitatMode.native
        )
    if mode == HabitatMode.native:
        if image is not None:
            raise typer.BadParameter("--sandbox-image requires --habitat sbx.")
        return None
    if target == DeploymentTarget.docker:
        raise typer.BadParameter(
            "sbx setup requires a host service; Docker service integration is not supported."
        )
    if any(name not in AGENT_SERVICES for name in agents):
        raise typer.BadParameter(
            "sbx setup supports Claude, Codex and DeepSeek; choose native for other agents."
        )
    if image is None and interactive:
        image = Prompt.ask(
            "Sandbox agent image (repository@sha256:digest)", console=console
        )
    if image is None or re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is None:
        raise typer.BadParameter(
            "sbx requires --sandbox-image with an immutable repository@sha256:digest reference."
        )
    return image
