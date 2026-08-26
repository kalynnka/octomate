"""`octomate mcp ...` — the gateway's static MCP client config, written per
runtime by one verb-first command group: `octomate mcp install --agent claude
--agent codex` configures every selected runtime in one run. The module
Octomate serves is the unit; the agents are selectors on it.

The three facts every install writes live here — where the gateway is, the
credential, which runtime the calls are from — so the runtimes cannot drift
apart. The server holds the same literals (`octomate.mcp.gateway`,
`octomate.types.threads`); the CLI cannot import that half, so the tests are
what hold the two together. Unlike the hook commands — whose scripts resolve
the address and credential from the environment when each hook fires — a
static MCP entry is read by the runtime itself, so both are resolved once, at
install time, and written into the file: the file holds the literal
credential, and rotating it means re-running install.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import tomlkit
import typer
from tomlkit.items import Table

from octomate_cli.claude import Scope, load_settings, write_settings
from octomate_cli.config import (
    OCTOMATE_URL_ENV,
    SECRET_ENV,
    resolved_secret,
    resolved_url,
)
from octomate_cli.deepseek import (
    dsh_home,
    patch_file,
    patch_text_with_block,
    patch_text_without_block,
)
from octomate_cli.hooks import announce_secret

# The served gateway's endpoint under the base URL: the server mounts each MCP
# server at `/<name>` + its `mcp_path`, and the gateway's name is `gateway`.
GATEWAY_MCP_PATH = "/gateway/mcp"

# The entry name every client file mounts the server under — also dsh's
# `serverName` — so each runtime names the tools `mcp__gateway__<spell>`.
GATEWAY_SERVER_KEY = "gateway"

# The header a native session's calls attribute their runtime with, and the
# value each runtime's install writes. Attribution within the bearer's trust
# domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"
CLAUDE_NATIVE_CLIENT = "claude-native"
CODEX_NATIVE_CLIENT = "codex-native"
DEEPSEEK_NATIVE_CLIENT = "deepseek-native"

# The MCP client bridge dsh mounts a server through, one patch row per server —
# part of dsh's own module closure, unlike the hooks bridge, so no --bridge
# link step. The row's id and markers are what a re-install overwrites and an
# uninstall removes, without ever touching the hooks block.
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
GATEWAY_ROW_ID = "octomate-gateway"
GATEWAY_MARK_BEGIN = "# >>> octomate deepseek gateway >>>"
GATEWAY_MARK_END = "# <<< octomate deepseek gateway <<<"


class McpAgent(str, Enum):
    claude = "claude"
    codex = "codex"
    deepseek = "deepseek"


mcp_typer = typer.Typer(
    help="Manage the gateway MCP config native sessions route through.",
    no_args_is_help=True,
)

AgentsOption = Annotated[
    list[McpAgent] | None,
    typer.Option(
        "--agent", help="Which runtime(s) to configure; repeat for several at once."
    ),
]
McpScopeOption = Annotated[
    Scope,
    typer.Option(
        help="claude only — which config to touch: 'user' (~/.claude.json) or "
        "'project' (./.mcp.json)."
    ),
]
ClaudeFileOption = Annotated[
    Path | None,
    typer.Option(
        "--claude-file",
        help="claude only — explicit config path; overrides --scope when given.",
    ),
]
CodexConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--codex-config",
        help="codex only — explicit config.toml path; defaults to "
        "~/.codex/config.toml.",
    ),
]
DshHomeOption = Annotated[
    Path | None,
    typer.Option(
        "--dsh-home",
        help="deepseek only — the dsh home; defaults to $DSH_HOME or ~/.dsh.",
    ),
]


def picked_agents(agents: list[McpAgent] | None) -> list[McpAgent]:
    if not agents:
        raise typer.BadParameter("name at least one --agent: claude, codex, deepseek")
    return list(dict.fromkeys(agents))


def gateway_url(url: str | None) -> str:
    """The full `/gateway/mcp` URL an install writes, from the pinned base or the
    client's own resolution — refused when nothing names an address, since a
    static entry pointing nowhere would fail every session's tool listing."""
    base = url if url is not None else resolved_url()
    if base is None:
        raise typer.BadParameter(
            f"no --url given, {OCTOMATE_URL_ENV} is unset, and no cli.toml names "
            "a url — a static MCP entry needs a concrete address; run "
            "`octomate configure --url http://<host>:<port>`"
        )
    return base.rstrip("/") + GATEWAY_MCP_PATH


def gateway_secret() -> str:
    """The credential an install embeds in an entry's Authorization header —
    refused when nothing resolves, since the entry would 401 on every call."""
    secret = resolved_secret()
    if secret is None:
        raise typer.BadParameter(
            f"no credential resolves — {SECRET_ENV} is unset and no cli.toml "
            "holds one; run `octomate configure` first. The entry embeds the "
            "literal credential, so installing without one would only 401."
        )
    return secret


def claude_file(scope: Scope, file: Path | None) -> Path:
    if file is not None:
        return file
    if scope is Scope.user:
        return Path.home() / ".claude.json"
    return Path.cwd() / ".mcp.json"


def codex_config(path: Path | None) -> Path:
    return path if path is not None else Path.home() / ".codex" / "config.toml"


def load_toml(path: Path) -> tomlkit.TOMLDocument:
    """The whole document, comments and all — tomlkit's round-trip is what keeps
    the operator's file theirs."""
    return tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()


def gateway_patch_block(url: str, secret: str) -> str:
    """The marker-delimited row mounting dsh's MCP client on the served gateway —
    `serverName: gateway`, so the model sees `mcp__gateway__<spell>`, the same
    names Claude and Codex read. Header values are JSON-quoted, which YAML reads
    as flow scalars: the credential is hand-written and need not be YAML-safe."""
    return (
        f"{GATEWAY_MARK_BEGIN}\n"
        f"- insert:\n"
        f"    - id: {GATEWAY_ROW_ID}\n"
        f"      name: '{MCP_CLIENT_PACKAGE}'\n"
        f"      config:\n"
        f"        serverName: {GATEWAY_SERVER_KEY}\n"
        f"        transport: streamable-http\n"
        f"        url: {json.dumps(url)}\n"
        f"        headers:\n"
        f"          Authorization: {json.dumps(f'Bearer {secret}')}\n"
        f"          {CLIENT_HEADER}: {DEEPSEEK_NATIVE_CLIENT}\n"
        f"{GATEWAY_MARK_END}\n"
    )


@mcp_typer.command("install")
def install(
    agent: AgentsOption = None,
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate's base URL (http://host:port) to write; defaults to "
            f"${OCTOMATE_URL_ENV}, then cli.toml."
        ),
    ] = None,
    scope: McpScopeOption = Scope.user,
    claude_file_path: ClaudeFileOption = None,
    codex_config_path: CodexConfigOption = None,
    dsh_home_path: DshHomeOption = None,
) -> None:
    """Point the selected runtimes' native sessions at the served gateway.

    One command, several runtimes: each --agent gets its own file written —
    `mcpServers.gateway` in ~/.claude.json (or ./.mcp.json), an
    `mcp_servers.gateway` table in ~/.codex/config.toml (rich would eat the
    TOML header's brackets as markup), a dsh-mcp-client row in
    $DSH_HOME/cordis.patch.yml. The address and credential resolve once,
    now, and land in the files: they hold the literal credential — Codex's
    names the environment variable instead, resolved per session — and
    rotating it means re-running install. Re-running replaces each entry in
    place, everything else in each file kept.
    """
    picked = picked_agents(agent)
    target = gateway_url(url)
    # Everything refusable resolves before the first write, so a multi-agent
    # install never half-lands.
    embedded_secret = (
        gateway_secret()
        if McpAgent.claude in picked or McpAgent.deepseek in picked
        else None
    )
    for chosen in picked:
        if chosen is McpAgent.codex:
            path = codex_config(codex_config_path)
            document = load_toml(path)
            servers = document.get("mcp_servers")
            if servers is None:
                servers = tomlkit.table(is_super_table=True)
                document["mcp_servers"] = servers
            if not isinstance(servers, Table):
                raise typer.BadParameter(
                    f"{path} has a non-table 'mcp_servers' section"
                )
            entry = tomlkit.table()
            entry["url"] = target
            entry["bearer_token_env_var"] = SECRET_ENV
            headers = tomlkit.inline_table()
            headers[CLIENT_HEADER] = CODEX_NATIVE_CLIENT
            entry["http_headers"] = headers
            servers[GATEWAY_SERVER_KEY] = entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tomlkit.dumps(document))
            typer.echo(f"codex: installed the gateway MCP entry → {path}")
            typer.echo(
                f"  auth: ${{{SECRET_ENV}}} from each session's environment — "
                "export it in your shell profile"
            )
            announce_secret()
            continue
        if embedded_secret is None:
            raise RuntimeError("embedding installs resolve the credential up front")
        if chosen is McpAgent.claude:
            path = claude_file(scope, claude_file_path)
            document = load_settings(path)
            servers = document.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise typer.BadParameter(
                    f"{path} has a non-object 'mcpServers' section"
                )
            servers[GATEWAY_SERVER_KEY] = {
                "type": "http",
                "url": target,
                "headers": {
                    "Authorization": f"Bearer {embedded_secret}",
                    CLIENT_HEADER: CLAUDE_NATIVE_CLIENT,
                },
            }
            write_settings(path, document)
            typer.echo(f"claude: installed the gateway MCP entry → {path}")
        else:
            patch = patch_file(dsh_home(dsh_home_path))
            text = patch.read_text() if patch.exists() else "[]\n"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(
                patch_text_with_block(
                    text,
                    gateway_patch_block(target, embedded_secret),
                    GATEWAY_MARK_BEGIN,
                    GATEWAY_MARK_END,
                )
            )
            typer.echo(f"deepseek: installed the gateway MCP row → {patch}")
            typer.echo("  restart dsh (the web daemon included) to load it")
    typer.echo(f"Gateway: {target}")
    if embedded_secret is not None:
        typer.echo(
            "The files hold the literal credential; rotation means re-running install."
        )


@mcp_typer.command("uninstall")
def uninstall(
    agent: AgentsOption = None,
    scope: McpScopeOption = Scope.user,
    claude_file_path: ClaudeFileOption = None,
    codex_config_path: CodexConfigOption = None,
    dsh_home_path: DshHomeOption = None,
) -> None:
    """Remove the selected runtimes' gateway MCP entries, leaving everything
    else in each file — other servers, the operator's comments, the hooks row."""
    for chosen in picked_agents(agent):
        if chosen is McpAgent.claude:
            path = claude_file(scope, claude_file_path)
            document = load_settings(path)
            servers = document.get("mcpServers")
            if not isinstance(servers, dict) or GATEWAY_SERVER_KEY not in servers:
                typer.echo(f"claude: no gateway MCP entry in {path}")
                continue
            del servers[GATEWAY_SERVER_KEY]
            if not servers:
                del document["mcpServers"]
            write_settings(path, document)
            typer.echo(f"claude: removed the gateway MCP entry from {path}")
        elif chosen is McpAgent.codex:
            path = codex_config(codex_config_path)
            document = load_toml(path)
            servers = document.get("mcp_servers")
            if not isinstance(servers, Table) or GATEWAY_SERVER_KEY not in servers:
                typer.echo(f"codex: no gateway MCP entry in {path}")
                continue
            del servers[GATEWAY_SERVER_KEY]
            if len(servers) == 0:
                del document["mcp_servers"]
            path.write_text(tomlkit.dumps(document))
            typer.echo(f"codex: removed the gateway MCP entry from {path}")
        else:
            patch = patch_file(dsh_home(dsh_home_path))
            if not patch.exists():
                typer.echo(f"deepseek: no gateway MCP row in {patch}")
                continue
            patch.write_text(
                patch_text_without_block(
                    patch.read_text(), GATEWAY_MARK_BEGIN, GATEWAY_MARK_END
                )
            )
            typer.echo(f"deepseek: removed the gateway MCP row from {patch}")


@mcp_typer.command("show")
def show(
    agent: AgentsOption = None,
    scope: McpScopeOption = Scope.user,
    claude_file_path: ClaudeFileOption = None,
    codex_config_path: CodexConfigOption = None,
    dsh_home_path: DshHomeOption = None,
) -> None:
    """Show the selected runtimes' gateway MCP entries, credentials masked."""
    for chosen in picked_agents(agent):
        if chosen is McpAgent.claude:
            path = claude_file(scope, claude_file_path)
            servers = load_settings(path).get("mcpServers")
            entry = (
                servers.get(GATEWAY_SERVER_KEY) if isinstance(servers, dict) else None
            )
            if not isinstance(entry, dict):
                typer.echo(f"claude: no gateway MCP entry in {path}")
                continue
            headers = entry.get("headers")
            client = headers.get(CLIENT_HEADER) if isinstance(headers, dict) else None
            typer.echo(f"claude: gateway MCP entry in {path}")
            typer.echo(f"  url:    {entry.get('url')}")
            typer.echo(f"  client: {client}")
            typer.echo("  auth:   Bearer ***")
        elif chosen is McpAgent.codex:
            path = codex_config(codex_config_path)
            servers = load_toml(path).get("mcp_servers")
            entry = (
                servers.get(GATEWAY_SERVER_KEY) if isinstance(servers, Table) else None
            )
            if not isinstance(entry, Table):
                typer.echo(f"codex: no gateway MCP entry in {path}")
                continue
            headers = entry.get("http_headers")
            client = headers.get(CLIENT_HEADER) if isinstance(headers, dict) else None
            typer.echo(f"codex: gateway MCP entry in {path}")
            typer.echo(f"  url:    {entry.get('url')}")
            typer.echo(f"  auth:   ${{{entry.get('bearer_token_env_var')}}}")
            typer.echo(f"  client: {client}")
        else:
            patch = patch_file(dsh_home(dsh_home_path))
            text = patch.read_text() if patch.exists() else ""
            lines = text.splitlines()
            if GATEWAY_MARK_BEGIN not in (line.strip() for line in lines):
                typer.echo(f"deepseek: no gateway MCP row in {patch}")
                continue
            typer.echo(f"deepseek: gateway MCP row in {patch}")
            inside = False
            for line in lines:
                stripped = line.strip()
                if stripped == GATEWAY_MARK_BEGIN:
                    inside = True
                    continue
                if stripped == GATEWAY_MARK_END:
                    break
                if inside:
                    if stripped.startswith("Authorization:"):
                        indent = line[: len(line) - len(line.lstrip())]
                        line = f'{indent}Authorization: "Bearer ***"'
                    typer.echo(f"  {line}")
