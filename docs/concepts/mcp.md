# MCP design

Octomate has two MCP roles: it serves a stable set of tools to agents, and it
forwards requests to the connectors installed by a user. The
[usage guides](../usage/mcp/octomate.md) cover calling those tools;
[Tentacles](../tentacles/mcp.md) covers configuring upstream services.

## One tool interface across runtimes

The served tools cover routing, history, connector management and authorisation.
Harness agents use their MCP connection; Inkling receives the corresponding
capabilities in process. The tool descriptions share their contracts so the
runtime adapters do not invent different routing or access rules.

The HTTP server is at `/octomate/mcp`. An API token with the `mcp` scope identifies
the user, while client metadata identifies the runtime or active conversation:

| Metadata | Use |
|---|---|
| `X-Octomate-Client` | A native runtime, written by the client's MCP installer |
| `X-Octomate-Conversation` | A driven conversation, checked against the active run and its bearer |

The client or runtime integration sets these values; they are not tool arguments
chosen by the model. Native clients use the user's configured token. Driven Codex
receives a temporary key scoped to MCP, revoked when its client closes.
Driven Claude Code uses an in-process server;
driven DeepSeek Harness has no Octomate tool connection yet.

## Discover and call upstream tools

Installed tools are not individually re-exported. An agent discovers connectors
with `mcp_list_servers`, loads a connector's tool schemas with `mcp_list_tools`,
then invokes a tool through `mcp_call_tool`. The upstream validates its arguments.
This keeps each person's changing tool catalog out of the shared prompt prefix.

An operator's tentacle declaration is an offering; an installation belongs to a
user. OAuth grants belong to that user too. A `bare` offering with an operator
token instead gives callers a shared upstream identity. The installation boundary
does not make a shared token a personal grant.

## Authorisation and connections

OAuth grants are encrypted with the deployment's encryption key. Authorisation
links and device codes are delivered through the authenticated console or a
private channel surface, rather than returned to the model. Linking a channel
profile identifies the Octomate user; it is separate from granting access to an
upstream service.

Clients are pooled per user and installation. They close after the configured
idle timeout, and disabling or uninstalling a connector closes its cached client.
Endpoint and redirect checks keep stored credentials tied to the intended
service. Configured `bare` endpoints support local services; OAuth endpoints
require HTTPS and public addresses.

See the [MCP API reference](../api/mcp.md) for transport, authentication and OAuth
contracts, and the [capability reference](../api/capabilities.md) for the tools
used by Inkling.
