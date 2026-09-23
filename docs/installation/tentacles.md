# The tentacle registry

Agents, channels and MCP connectors are all **tentacles**. Declare them under
`tentacles:` in `config/tentacles.yaml`. Each key is an id you choose, and `type`
selects the implementation. The setup generator writes the initial map for you.

## Start with an agent and the console

This is a complete minimal registry for Claude Code and Trunkline:

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default
  trunkline:
    type: trunkline
    agents: [claude]
```

The agent supplies its own models and login. Add Trunkline's `static_dir` after
[building the console](server.md#build-trunkline). Server authentication belongs
in `auth.yaml` or environment variables, as described in [Server settings](settings.md).

For Codex, use `type: codex`, `permission_mode: user_review` and update the
channel's `agents` list to name its id. Once the first agent works, you can add the
others:

| Kind | `type` values | Reference |
|---|---|---|
| Agent | `claude`, `codex`, `deepseek`, `inkling` | [Agents](../usage/agents/index.md) |
| Channel | `slack`, `lark`, `discord`, `trunkline` | [Channels](../usage/channels/index.md) |
| MCP | `bare`, `oauth`, `oauth_discovery` | [MCP proxy](../usage/mcp/proxy.md) |

## Add a channel

Create the platform's bot first using its [channel guide](../usage/channels/index.md).
Then add a block under the **existing** `tentacles:` key. For example, a Discord
bot using your existing Claude agent:

```yaml
  discord:
    type: discord
    agents: [claude]
```

Set `bot_token` in that YAML block or provide
`OCTOMATE__TENTACLES__DISCORD__BOT_TOKEN` in the environment. If using `.env`,
remove the YAML placeholder. If the generator already made this block, change
`enabled: false` to `enabled: true` when the credential is ready.
Even disabled blocks must have the fields required by their type.

The first agent in `agents` answers new conversations by default; later entries
are additional available agents. Trunkline uses that order for its entry choices
and also exposes the registered agents' model catalogs.

## Add an MCP connector

Configure the [OAuth origin and encryption key](settings.md#profile-linking-and-mcp-authorisation)
first when the connector needs them. The CLI can generate GitHub's template on
macOS, Linux or WSL:

```sh
cd "$OCTOMATE_INSTALL_ROOT"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
octomate mcp preset github --client-id '<your-oauth-app-client-id>'
```

This adds a `github` entry to `config/tentacles.yaml` with the provider endpoints
and scopes. It refuses to replace an existing id. Follow its instructions for the
client secret and callback registration, then restart the server.

A template offers a connector; it does not install or authorise it for every
person. Each user installs it and completes consent through the
[MCP proxy flow](../usage/mcp/proxy.md). Other remote services can be configured
with the `bare`, `oauth` or `oauth_discovery` types.

## Registry rules

Rules the loader enforces:

- **Each agent type is declared once**, enabled or not. An agent runtime owns a
  fixed hook route such as `/hooks/claude`, so two Claude tentacles cannot coexist.
- **Channel and MCP types repeat freely.** Two Lark apps are two keys with
  `type: lark`, and two separate sets of threads.
- **Every channel names at least one agent**, as a list of tentacle ids. The first
  answers new conversations with its harness's default model. Every entry must be
  an enabled agent tentacle; the loader reports every bad entry at once.
- **Inkling needs at least one model.** Claude, Codex and DeepSeek Harness supply
  their own catalogs.
- **Every tentacle has `enabled`**, default `true`. A disabled tentacle keeps its
  block and is still validated, but does not connect.

The key is the tentacle's identity everywhere downstream. A stored profile names the
channel it came from by this key, a thread records it as its origin, and MCP
installations are recorded against it. Keep ids stable: changing a YAML key does
not migrate the stored profiles, threads or connector installations to the new id.

Many configuration models ignore unknown keys, though some, such as
`oauth_discovery`, reject them. The older `agents:`,
`channels:` and `mcp:` top-level blocks are unknown keys today, so a home that still
uses them boots with no tentacles at all. Two things are refused outright: a
tentacle block with no `type`, and a Codex block with the obsolete `sandbox` key,
which is now part of `permission_mode`.

Run the [configuration check](configuration.md#validate-without-starting) after
editing, then restart the server. Validate one real conversation or connector call
before moving on to the next integration.
