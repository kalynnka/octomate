"""Configure provider credentials and domain approvals in sbx's own store."""

from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt

from octomate_cli import sbx

console = Console(stderr=True)


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


# Typer does not resolve PEP 695 aliases in command parameters.
Service = Literal["anthropic", "openai", "deepseek"]

CREDENTIAL_APPROVAL_DOMAINS: dict[Service, dict[str, list[str]]] = {
    "openai": {
        "oauth": ["auth.openai.com", "chatgpt.com"],
        "apiKey": ["api.openai.com"],
    },
    "anthropic": {"oauth": ["platform.claude.com", "api.anthropic.com"]},
    "deepseek": {"apiKey": ["api.deepseek.com"]},
}


def credential_step(executable: str, service: Service, *, check: bool) -> str:
    inventory = SecretInventory.model_validate_json(
        sbx.run_sbx(executable, "secret", "ls", "--global", "--json", capture=True)
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
    sbx.run_sbx(
        executable,
        "secret",
        "set",
        service,
        *(["--oauth"] if selected == "oauth" else []),
    )
    return credential_step(executable, service, check=True)


def setup(
    backend: Annotated[Literal["sbx"], typer.Argument(help="Credential backend.")],
    service: Annotated[Service, typer.Option(help="Provider service to configure.")],
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Verify credentials and approvals without prompts or changes.",
        ),
    ] = False,
) -> None:
    """Configure one provider without changing habitat or Docker login settings.

    Credentials stay in sbx's store. Domain approvals are merged into
    ~/.config/sbx/credentials.yaml after confirmation.
    """
    executable = sbx.executable()
    path = Path.home() / ".config/sbx/credentials.yaml"
    try:
        document = SbxBindings.model_validate(
            yaml.safe_load(path.read_text()) if path.exists() else {}
        )
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise typer.BadParameter(
            f"Cannot read sbx bindings at {path}: {error}"
        ) from error
    mode = credential_step(executable, service, check=check)
    bindings = document.bindings.get(service, ServiceBindings())
    binding = bindings.oauth if mode == "oauth" else bindings.apiKey
    domains = CREDENTIAL_APPROVAL_DOMAINS[service][mode]
    missing = [
        domain for domain in domains if binding is None or domain not in binding.domains
    ]
    if not missing:
        console.print(
            f"{service} credentials and domain approvals are ready. Model access has not been tested."
        )
        return
    if check:
        raise typer.BadParameter(
            f"Missing {service} {mode} approvals: {', '.join(missing)}. Run `octomate habitat credentials sbx --service {service}`."
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document.model_dump(exclude_unset=True), sort_keys=False)
    )
    console.print(f"Updated {path}")
    console.print(
        f"{service} credentials and domain approvals are ready. Model access has not been tested."
    )
