"""Generate local MCP tentacle configuration from provider presets."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, Literal

import typer
import yaml
from octomate_protocol.config import config_home
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from rich.console import Console
from rich.prompt import Prompt

mcp_typer = typer.Typer(help="Configure MCP tentacles.", no_args_is_help=True)


class McpPreset(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    provider: Literal["github"]
    name: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    read_only: bool = False

    def configuration(self) -> dict[str, JsonValue]:
        template = Template(
            files("octomate_cli")
            .joinpath("presets", f"{self.provider}.yaml")
            .read_text()
        )
        rendered = template.substitute(
            name=json.dumps(self.name),
            client_id=json.dumps(self.client_id),
            endpoint="readonly" if self.read_only else "",
        )
        return TypeAdapter(dict[str, dict[str, dict[str, JsonValue]]]).validate_python(
            yaml.safe_load(rendered)
        )["tentacles"][self.name]


@mcp_typer.command()
def preset(
    provider: Annotated[Literal["github"], typer.Argument(help="Provider preset.")],
    client_id: Annotated[
        str | None,
        typer.Option(
            help="OAuth application client ID; enable Device Flow in the app."
        ),
    ] = None,
    name: Annotated[str | None, typer.Option(help="Tentacle ID.")] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            help="Destination YAML; defaults to tentacles.yaml in the config home."
        ),
    ] = None,
    read_only: Annotated[
        bool,
        typer.Option(
            help="Use GitHub's read-only MCP endpoint. OAuth scopes still permit repository writes."
        ),
    ] = False,
) -> None:
    """Save an MCP tentacle preset with endpoints and scopes filled in."""
    if client_id is None:
        client_id = Prompt.ask(
            "OAuth application client ID", console=Console(stderr=True)
        )
    if not client_id.strip() or (name is not None and not name.strip()):
        raise typer.BadParameter("Client ID and tentacle name must not be empty.")
    selection = McpPreset(
        provider=provider,
        name=name or provider,
        client_id=client_id,
        read_only=read_only,
    )
    path = (
        output.expanduser() if output is not None else config_home() / "tentacles.yaml"
    )
    adapter = TypeAdapter(dict[str, JsonValue])
    existing = yaml.safe_load(path.read_text()) if path.exists() else None
    config = adapter.validate_python(existing if existing is not None else {})
    mcps = adapter.validate_python(config.get("tentacles", {}))
    tentacle_id = selection.name
    if tentacle_id in mcps:
        raise typer.BadParameter(
            f"MCP tentacle {tentacle_id!r} already exists in {path}."
        )
    mcps[tentacle_id] = selection.configuration()
    config["tentacles"] = mcps
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    typer.echo(f"Saved MCP tentacle {tentacle_id!r} to {path.resolve()}")
    typer.echo(
        f"Set OCTOMATE__TENTACLES__{tentacle_id.upper()}__CLIENT_SECRET in your .env "
        "and configure oauth.callback_base_uri. Register "
        f"<callback_base_uri>/oauth/{tentacle_id}/callback in the GitHub OAuth App."
    )
