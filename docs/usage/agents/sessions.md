# Driven and native sessions

## What a driven session disables

A run started from a channel runs on the server as the service account. It must
speak as the person who asked and nobody else, so each harness is launched with its
local customisations off:

| Agent | Disabled | Provided instead |
|---|---|---|
| Claude Code | User settings, hooks, plugins and skills (safe mode), every local MCP server | Octomate's MCP server, in process, and its instructions appended to the system prompt |
| Codex | Hooks, plugins, apps, notifications, every local MCP server | Octomate's MCP server over HTTP with a temporary per-conversation token |
| DeepSeek Harness | Plugins, hooks, MCP servers and profile patches, by running with a fresh home | Nothing. A driven dsh turn has no Octomate tools yet |
| Inkling | Not applicable | Its capabilities, the user's MCP connectors, and file and shell tools rooted in the workspace |

Settings, credentials and sessions are still shared where the harness keeps them
apart from customisation: Claude's login, Codex's login, dsh's settings and
credentials files.

## Native sessions

Claude Code, Codex and DeepSeek Harness each serve a hook router at
`/hooks/<runtime>` and a transcript stream at `/hooks/<runtime>/stream`. A session
you run yourself, with the [client installed](../../installation/clients/index.md),
lands
as a thread on a pseudo-channel named after the runtime, `claude-native` and so on,
owned by the user whose token the client presents. A session started under a
declared project root is filed under that project, when the client is on the
server's own machine.

The hooks carry the human ledger: the prompt at `UserPromptSubmit`, the answer at
`Stop`. The tail carries everything else: tool calls, thinking, subagents, usage.
Without a tail, a session keeps only the hooks' sketch. Nothing sweeps for sessions
that ran while the server was down; the next prompt's launcher catches a session up.

A native session can call the routing spells over MCP, with limits: it can be
summoned away from and schemed into someone's direct messages, but it cannot be
teleported, and it has no workspace to dispel. See
[Moving a conversation](../gateway.md).

## Telemetry

Each harness agent has an `instrument` flag that exports its native spans under
Octomate's trace. See [Observability](../../installation/observability.md).
