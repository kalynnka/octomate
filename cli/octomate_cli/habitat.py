"""Host setup for habitats, shared by standalone commands and the service wizard."""

import os
import re
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rich.prompt import Confirm, Prompt

from octomate_cli.wizard.base import console

habitat_typer = typer.Typer(help="Prepare the service account's agent habitats.")


class HabitatMode(StrEnum):
    native = "native"
    sbx = "sbx"


class CredentialBinding(BaseModel):
    model_config = ConfigDict(extra="allow")

    domains: list[str] = Field(default_factory=list)


class ServiceBindings(BaseModel):
    model_config = ConfigDict(extra="allow")

    oauth: CredentialBinding | None = None
    apiKey: CredentialBinding | None = None


class SbxBindings(BaseModel):
    model_config = ConfigDict(extra="allow")

    bindings: dict[str, ServiceBindings] = Field(default_factory=dict)


class StoredSecret(BaseModel):
    scope: str
    type: str
    name: str
    secret: str  # sbx returns a description, never the credential value.


class SecretInventory(BaseModel):
    secrets: list[StoredSecret]


type Service = Literal["anthropic", "openai", "deepseek"]

SBX_AGENTS = frozenset({"claude", "codex", "deepseek", "acp"})
AGENT_SERVICES: dict[str, Service] = {
    "claude": "anthropic",
    "codex": "openai",
    "deepseek": "deepseek",
}
CREDENTIAL_APPROVAL_DOMAINS: dict[Service, dict[str, list[str]]] = {
    "openai": {
        "oauth": ["auth.openai.com", "chatgpt.com"],
        "apiKey": ["api.openai.com"],
    },
    "anthropic": {"oauth": ["platform.claude.com", "api.anthropic.com"]},
    "deepseek": {"apiKey": ["api.deepseek.com"]},
}


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


def credential_step(executable: str, service: Service, *, check: bool) -> str:
    inventory = SecretInventory.model_validate_json(
        run_sbx(executable, "secret", "ls", "--global", "--json", capture=True)
    )
    stored = next(
        (
            item
            for item in inventory.secrets
            if item.scope == "global"
            and item.type == "service"
            and item.name == service
        ),
        None,
    )
    mode = (
        ("oauth" if stored.secret == "(oauth configured)" else "apiKey")
        if stored
        else None
    )
    if check:
        if mode not in CREDENTIAL_APPROVAL_DOMAINS[service]:
            raise typer.BadParameter(f"No supported sbx credential for {service}.")
        return mode
    if service == "anthropic":
        if mode != "oauth":
            raise typer.BadParameter(
                "Claude needs an existing sbx OAuth login. Run `sbx run claude` "
                "interactively to authenticate, then rerun setup. "
                "sbx 0.48's `secret set --oauth` supports OpenAI only."
            )
        console.print("Claude: using the existing sbx OAuth login.")
        return mode
    choices = list(CREDENTIAL_APPROVAL_DOMAINS[service])
    if mode is not None:
        choices.insert(0, "keep")
        console.print(f"{service}: stored {mode} credential found.")
    selected = Prompt.ask(
        f"{service} authentication",
        choices=choices,
        default=choices[0],
        console=console,
    )
    if selected == "keep" and mode is not None:
        return mode
    run_sbx(
        executable,
        "secret",
        "set",
        service,
        *(["--oauth"] if selected == "oauth" else []),
    )
    return credential_step(executable, service, check=True)


@habitat_typer.command()
def setup(
    backend: Annotated[
        Literal["sbx"], typer.Argument(help="Habitat backend to set up.")
    ],
    agent: Annotated[
        list[str] | None,
        typer.Option(
            "--agent", help="claude, codex, deepseek or acp; repeat for several."
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Verify existing setup without prompts or changes."
        ),
    ] = False,
) -> None:
    """Set up sbx login and provider approvals as the service's OS account.

    Credentials stay in sbx's store. This command merges domain approvals into
    ~/.config/sbx/credentials.yaml; it does not start agents or activate a service.
    """
    if sys.platform not in {"darwin", "linux"} or os.getuid() == 0:
        raise typer.BadParameter(
            "Run on macOS or Linux as the service account, without sudo."
        )
    if agent is None:
        if check:
            raise typer.BadParameter("--check requires --agent.")
        agent = Prompt.ask(
            "Agents using sbx (comma-separated)", default="codex", console=console
        ).split(",")
    agents = list(dict.fromkeys(name.strip() for name in agent))
    if not agents or any(name not in SBX_AGENTS for name in agents):
        raise typer.BadParameter("sbx setup supports claude, codex, deepseek and acp.")
    executable = shutil.which("sbx")
    if executable is None:
        raise typer.BadParameter(
            "Install sbx 0.48.0, then rerun this command: https://docs.docker.com/ai/sandboxes/install/"
        )
    version = run_sbx(executable, "version", capture=True)
    if re.search(r"\bv0\.48\.0(?:\s|$)", version) is None:
        raise typer.BadParameter("This Octomate version requires sbx 0.48.0.")
    path = Path.home() / ".config/sbx/credentials.yaml"
    try:
        document = SbxBindings.model_validate(
            yaml.safe_load(path.read_text()) if path.exists() else {}
        )
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise typer.BadParameter(
            f"Cannot read sbx bindings at {path}: {error}"
        ) from error
    console.print(f"sbx setup for {Path.home()} · agents: {', '.join(agents)}")
    if not check and Confirm.ask(
        "Sign in to Docker with sbx now?", default=False, console=console
    ):
        run_sbx(executable, "login")
    run_sbx(executable, "ls", "--quiet", capture=True)
    forwarding = run_sbx(
        executable, "settings", "get", "ssh.agentForwardingEnabled", capture=True
    )
    if forwarding.strip() != "false":
        raise typer.BadParameter(
            "Octomate requires SSH agent forwarding off because it would let "
            "processes in users' habitats authenticate with SSH identities loaded "
            "in the host service account's agent. Those identities must not be "
            "shared across habitat users. Run "
            "`sbx settings set ssh.agentForwardingEnabled false`, then "
            "`sbx daemon restart` before setup."
        )
    changed = False
    for name in agents:
        service = AGENT_SERVICES.get(name)
        if service is None:
            console.print(
                "ACP: configure the selected provider's authentication separately; "
                "no credential service is inferred."
            )
            continue
        mode = credential_step(executable, service, check=check)
        bindings = document.bindings.get(service, ServiceBindings())
        binding = bindings.oauth if mode == "oauth" else bindings.apiKey
        domains = CREDENTIAL_APPROVAL_DOMAINS[service][mode]
        missing = [
            domain
            for domain in domains
            if binding is None or domain not in binding.domains
        ]
        if not missing:
            continue
        if check:
            raise typer.BadParameter(
                f"Missing {service} {mode} approvals: {', '.join(missing)}. Run `octomate habitat setup sbx --agent {name}`."
            )
        console.print(f"Approve {service} {mode}: {', '.join(missing)}\nFile: {path}")
        if not Confirm.ask(
            "Allow sbx to use this credential for these domains?",
            default=False,
            console=console,
        ):
            raise typer.Abort()
        binding = binding or CredentialBinding()
        binding.domains = [*binding.domains, *missing]
        if mode == "oauth":
            bindings.oauth = binding
        else:
            bindings.apiKey = binding
        document.bindings = {**document.bindings, service: bindings}
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(document.model_dump(exclude_unset=True), sort_keys=False)
        )
        console.print(f"Updated {path}")
    console.print(
        "sbx host setup and selected provider approvals are ready. "
        "Model access has not been tested."
    )
