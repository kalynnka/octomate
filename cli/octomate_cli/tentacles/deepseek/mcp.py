"""Install and manage the deepseek tentacle's MCP configuration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.tentacles.deepseek.config import (
    DshHomeOption,
    dsh_home,
    patch_file,
    patch_text_with_block,
    patch_text_without_block,
)
from octomate_cli.tentacles.mcp import (
    CLIENT_HEADER,
    OCTOMATE_SERVER_KEY,
    OAuthOption,
    octomate_secret,
    octomate_url,
)

mcp_typer = typer.Typer(
    help="Manage the Octomate MCP row for native dsh sessions.", no_args_is_help=True
)


# The MCP client bridge the Octomate row mounts — part of dsh's own module
# closure, unlike the hooks bridge, so no --bridge link step.
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
CONNECTOR_PACKAGE = "@octomate/dsh-connector"

ProfileOption = Annotated[
    str,
    typer.Option(
        help="DSH profile receiving the OAuth connector; requires Web, credentials, and MCP account services."
    ),
]
DshUrlOption = Annotated[
    str | None,
    typer.Option(
        help="DSH launch URL; defaults to DSH_API_URL, deepseek.url in cli.toml, then localhost:3080."
    ),
]


# The Octomate MCP row's id and markers: what a re-install overwrites and an
# uninstall removes, without ever touching the hooks block.
MCP_ROW_ID = "octomate-mcp"


MCP_MARK_BEGIN = "# >>> octomate deepseek mcp >>>"


MCP_MARK_END = "# <<< octomate deepseek mcp <<<"


LEGACY_MCP_MARK_BEGIN = "# >>> octomate deepseek gateway >>>"


LEGACY_MCP_MARK_END = "# <<< octomate deepseek gateway <<<"


def mcp_patch_block(url: str, secret: str) -> str:
    """The marker-delimited row mounting dsh's MCP client on the served server —
    `serverName: octomate`, so the model sees `mcp__octomate__<tool>`, the same
    names Claude and Codex read. Header values are JSON-quoted, which YAML reads
    as flow scalars: the credential is hand-written and need not be YAML-safe."""
    return (
        f"{MCP_MARK_BEGIN}\n"
        f"- insert:\n"
        f"    - id: {MCP_ROW_ID}\n"
        f"      name: '{MCP_CLIENT_PACKAGE}'\n"
        f"      config:\n"
        f"        serverName: {OCTOMATE_SERVER_KEY}\n"
        f"        transport: streamable-http\n"
        f"        url: {json.dumps(url)}\n"
        f"        headers:\n"
        f"          Authorization: {json.dumps(f'Bearer {secret}')}\n"
        f"          {CLIENT_HEADER}: deepseek\n"
        f"{MCP_MARK_END}\n"
    )


@mcp_typer.command("install")
def mcp_install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate's base URL (http://host:port) to write; defaults to "
            f"${CLISettings.env('url')}, then cli.toml."
        ),
    ] = None,
    home: DshHomeOption = None,
    oauth: OAuthOption = None,
    profile: ProfileOption = "web",
    dsh_url: DshUrlOption = None,
) -> None:
    """Install a native OAuth connector and start DSH login, or embed an API key.

    OAuth uses DSH's profile package manager, MCP manager, and credential store.
    API-key mode writes a standalone client into the shared Cordis patch.
    """
    target = octomate_url(url)
    secret = octomate_secret(oauth)
    target_home = dsh_home(home).resolve()
    if not profile or Path(profile).name != profile or profile in {".", ".."}:
        raise typer.BadParameter("DSH profile must be a profile name, not a path")
    connector = target_home / "octomate" / "mcp" / profile
    if secret is None:
        dsh_executable()
        connector.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).with_name("connector")
        for name in ["package.json", "index.mjs"]:
            shutil.copyfile(source / name, connector / name)
        (connector / "cordis.patch.yml").write_text(
            f"- insert:\n    - id: {MCP_ROW_ID}\n"
            f"      name: '{CONNECTOR_PACKAGE}'\n"
            f"      config:\n        url: {json.dumps(target)}\n"
        )
        patch = patch_file(target_home)
        if patch.exists():
            text = patch_text_without_block(
                patch.read_text(), MCP_MARK_BEGIN, MCP_MARK_END
            )
            patch.write_text(
                patch_text_without_block(
                    text, LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END
                )
            )
        plugin_command(target_home, profile, "add", str(connector))
        typer.secho(
            f"Installed Octomate OAuth connector in DSH profile {profile!r} → {target}",
            fg=typer.colors.CYAN,
            err=True,
        )
        mcp_login(dsh_url)
        return
    if connector.exists():
        plugin_command(target_home, profile, "remove", CONNECTOR_PACKAGE)
        shutil.rmtree(connector)
    patch = patch_file(dsh_home(home))
    text = patch.read_text() if patch.exists() else "[]\n"
    text = patch_text_without_block(text, LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        patch_text_with_block(
            text,
            mcp_patch_block(target, secret),
            MCP_MARK_BEGIN,
            MCP_MARK_END,
        )
    )
    typer.echo(f"Installed the Octomate MCP row → {target}")
    typer.echo(f"  patch:  {patch} (row id {MCP_ROW_ID!r})")
    typer.echo("  client: deepseek")
    typer.echo(
        "  auth:   embedded — the file holds the literal credential; rotation "
        "means re-running install"
    )
    typer.echo("Restart dsh (the web daemon included) to load the row.")


@mcp_typer.command("uninstall")
def mcp_uninstall(home: DshHomeOption = None, profile: ProfileOption = "web") -> None:
    """Remove the Octomate MCP row, leaving the hooks row and everything else in
    the patch file untouched."""
    target_home = dsh_home(home).resolve()
    if not profile or Path(profile).name != profile or profile in {".", ".."}:
        raise typer.BadParameter("DSH profile must be a profile name, not a path")
    connector = target_home / "octomate" / "mcp" / profile
    if connector.exists():
        plugin_command(target_home, profile, "remove", CONNECTOR_PACKAGE)
        shutil.rmtree(connector)
        typer.echo(f"Removed Octomate OAuth connector from DSH profile {profile!r}.")
    patch = patch_file(target_home)
    if not patch.exists():
        typer.echo(f"No Octomate MCP row in {patch}")
        raise typer.Exit()
    text = patch_text_without_block(patch.read_text(), MCP_MARK_BEGIN, MCP_MARK_END)
    patch.write_text(
        patch_text_without_block(text, LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    )
    typer.echo(f"Removed the Octomate MCP row from {patch}")


@mcp_typer.command("show")
def mcp_show(home: DshHomeOption = None, profile: ProfileOption = "web") -> None:
    """Show the Octomate MCP row, credential masked."""
    if not profile or Path(profile).name != profile or profile in {".", ".."}:
        raise typer.BadParameter("DSH profile must be a profile name, not a path")
    connector = dsh_home(home) / "octomate" / "mcp" / profile / "cordis.patch.yml"
    if connector.exists():
        typer.echo(f"Octomate OAuth connector in DSH profile {profile!r}: {connector}")
        typer.echo(connector.read_text())
        return
    patch = patch_file(dsh_home(home))
    text = patch.read_text() if patch.exists() else ""
    lines = text.splitlines()
    markers = (
        (MCP_MARK_BEGIN, MCP_MARK_END)
        if MCP_MARK_BEGIN in (line.strip() for line in lines)
        else (LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    )
    if markers[0] not in (line.strip() for line in lines):
        typer.echo(f"No Octomate MCP row in {patch}")
        raise typer.Exit()
    typer.echo(f"Octomate MCP row in {patch}:")
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == markers[0]:
            inside = True
            continue
        if stripped == markers[1]:
            break
        if inside:
            if stripped.startswith("Authorization:"):
                indent = line[: len(line) - len(line.lstrip())]
                line = f'{indent}Authorization: "Bearer ***"'
            typer.echo(f"  {line}")


@mcp_typer.command("login")
def mcp_login(dsh_url: DshUrlOption = None) -> None:
    """Authorize the installed connector on the running DSH host."""
    # Keep command discovery independent of the streaming and WebSocket clients.
    from pydantic import ValidationError
    from websockets.exceptions import InvalidStatus, WebSocketException

    from octomate_cli.streaming.deepseek import DshCompatibilityError
    from octomate_cli.tentacles.deepseek.oauth import login

    try:
        login(dsh_url)
    except InvalidStatus as error:
        status = error.response.status_code
        typer.secho(
            f"DSH rejected the authorization connection with HTTP {status}.",
            fg=typer.colors.RED,
            err=True,
        )
        typer.secho(
            "Configure DSH's current launch URL, including its token, with "
            "`octomate configure --dsh-url`, then retry "
            "`octomate deepseek mcp login`."
            if status in {401, 403}
            else "Check that deepseek.url points to the running DSH Web service.",
            dim=True,
            err=True,
        )
        raise typer.Exit(1) from None
    except (ValidationError, WebSocketException):
        typer.secho("DSH authorization protocol failed.", fg=typer.colors.RED, err=True)
        typer.secho(
            "Check the DSH launch URL and update DSH and Octomate together.",
            dim=True,
            err=True,
        )
        raise typer.Exit(1) from None
    except (DshCompatibilityError, OSError) as error:
        typer.secho(
            f"DSH OAuth login did not complete: {error}", fg=typer.colors.RED, err=True
        )
        typer.secho(
            "Start or restart the DSH profile containing the connector, configure its "
            "launch URL with `octomate configure --dsh-url`, then run "
            "`octomate deepseek mcp login`.",
            dim=True,
            err=True,
        )
        raise typer.Exit(1) from None


def plugin_command(home: Path, profile: str, action: str, package: str) -> None:
    """Let DSH own profile dependency installation and live bundle activation."""
    try:
        result = subprocess.run(
            [dsh_executable(), "plugin", "--profile", profile, action, package],
            env={**os.environ, "DSH_HOME": str(home)},
            check=False,
        )
    except FileNotFoundError:
        raise typer.BadParameter(
            "Could not start DSH; check deepseek.executable in cli.toml and the launcher interpreter on PATH"
        ) from None
    if result.returncode:
        raise typer.Exit(result.returncode)


def dsh_executable() -> str:
    """Resolve the configured native launcher before changing DSH configuration."""
    command = cli_settings().deepseek.executable
    executable = shutil.which(os.path.expanduser(command))
    if executable is None:
        raise typer.BadParameter(
            f"DSH executable {command!r} was not found or is not executable. "
            "Set deepseek.executable in cli.toml, or run "
            "`octomate configure --dsh-executable /path/to/dsh`."
        )
    return executable
