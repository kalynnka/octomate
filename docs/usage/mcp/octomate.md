# Octomate MCP

Octomate serves one MCP server, at `/octomate/mcp`, that offers every agent the same
three things: the routing spells, history search, and the person's own installed
MCP connectors. It is how a native Claude Code session in your terminal reaches the
same tools a driven run has.

## Who is speaking

The endpoint is streamable HTTP, stateless, and gated by an API token with the
`mcp` scope. The token says which **user**; a header says which **runtime**, and
both are written by configuration, never by the model:

| Header | Set by | Meaning |
|---|---|---|
| `X-Octomate-Client: claude-native`, `codex-native`, `deepseek-native` | `octomate <runtime> mcp install` | A native session speaking for the token's user |
| `X-Octomate-Conversation: <id>` | A driven Codex launch | A driven turn, answered only while that run is in flight and only for the bearer that kicked it |

A call with neither header is refused, with a sentence saying why. A deployment
with no registered user answers 401 to everything.

How each runtime gets there:

| Runtime | Connection | Tool names |
|---|---|---|
| Native Claude Code, Codex, dsh | HTTP, from the client's static entry | `mcp__octomate__<tool>`, or `mcp__octomate` for Codex |
| Driven Claude Code | The same server built in process for the turn | `mcp__octomate__<tool>` |
| Driven Codex | HTTP as server `octomate_driven`, with a temporary per-conversation token | `mcp__octomate_driven` |
| Inkling | The connector tools in process; routing and history are its own capabilities | Bare names |
| Driven dsh | Nothing yet | |

Native installation is covered in [Hooks and MCP](../../installation/clients/index.md).

## The tools

Listed in the order the server offers them.

**Gateway.** `gateway_scry`, `gateway_summon`, `gateway_teleport`, `gateway_scheme`,
`gateway_send`, `gateway_dispel`. What each does, and what a native session may
not do, is on [Moving a conversation](../gateway.md). Over MCP `send` delivers
inside the call, since there is no run stream to ride, and `teleport` answers with a
fixed sentence because the runtime cannot be suspended the way Inkling can.

**History.** `history_search`, `history_read_before`, `history_read_after`. See
[History](../history.md). Each requires the session to speak for a registered
user.

**Connectors.** `mcp_list_tentacles` lists what the operator configured;
`mcp_list_servers` lists what this user installed, with enabled and authorisation
state; `mcp_install`, `mcp_enable`, `mcp_disable`, `mcp_uninstall` manage them by
id; `mcp_list_tools` loads one connector's tool schemas and instructions;
`mcp_call_tool` calls one as the user. Management needs a registered user.

**Authorisation.** `oauth_connect` sends the user a private authorisation link or
device code for a connector; `oauth_confirm` checks, polling where the provider
requires it; `oauth_link_profile` sends the current channel user a private link to
attach this profile to an account. None of them returns a link to the model.

The server's instructions, sent with the tool list, tell the model when to route,
how to search history, and to check the installed connectors before concluding a
tool does not exist. The tool descriptions themselves are the same docstrings
Inkling's capabilities compile, so no two runtimes read a different contract.

## Tokens

A native client's token is one you issued and saved with `octomate configure`;
rotating it means reinstalling the MCP entry, which embeds it. A driven Codex run
gets a key minted for the asking user, scoped to `mcp`, expiring after
`auth.runtime_api_key_lifetime` (a day) and revoked when the process closes. The
user's plaintext never touches the model or the launch config; the header names
the conversation and the token lives in an environment variable the launch config
references.
