# Tentacles

Tentacles connect Octomate to your agents, channels and tools. Choose an agent
to do the work, a channel to reach it, and MCP connectors for the services it
needs. Start with the agent you already use and add connections as you need them.

## Agents

| Agent | What it connects | Before enabling |
|---|---|---|
| [Claude Code](../usage/agents/claude-code.md) | Claude Agent SDK for driven work; hooks and transcripts for native sessions | Authenticate Claude Code as the server user |
| [Codex](../usage/agents/codex.md) | Codex app-server for driven work; hooks and rollouts for native sessions | Authenticate Codex as the server user |
| [DeepSeek Harness](../usage/agents/deepseek.md) | A managed `dsh web` process and native-session collection | Install and configure `dsh`; this integration is experimental |
| [Inkling](../usage/agents/inkling.md) | Octomate's built-in Pydantic AI agent | Configure a model and its provider credentials |

Inkling is the backup option when you want a model supported by Pydantic AI
without a separate agent harness. It runs inside Octomate and has no native
sessions to collect. [Agent settings](../usage/agents/index.md) explain models,
routes and permissions.

## Channels

| Channel | Where you use it | Before enabling |
|---|---|---|
| [Slack](../usage/channels/slack.md) | Channels, threads, direct messages and the assistant pane | Create a Socket Mode app and supply its app and bot tokens |
| [Lark / Feishu](../usage/channels/lark.md) | Groups, threads and one-to-one chats | Create a bot app with a long connection and supply its app credentials |
| [Discord](../usage/channels/discord.md) | Server channels, public threads and direct messages | Add a bot to your server and supply its token |
| [Trunkline](../usage/channels/trunkline.md) | Octomate's web console | Build the frontend and configure sign-in; the console is in preview |

Each channel guide covers app setup, credentials and its presentation of messages
and approvals. [Compare channel capabilities](../usage/channels/index.md#what-each-channel-renders).

## MCP connectors

| Type | Connects to | Setup |
|---|---|---|
| `bare` | An MCP server with no authentication or one deployment token | Supply the endpoint and optional token |
| `oauth_discovery` | An MCP server that advertises its OAuth configuration | Supply the endpoint and configure Octomate's OAuth settings |
| `oauth` | An MCP server using an OAuth app you register | Supply the app credentials, scopes and supported flows |
| Slack with `mcp: true` | Slack tools acting as the linked person | Enable the channel's MCP option and user OAuth |

[Enable MCP connectors](mcp.md) covers these types and the GitHub CLI preset.
Each person installs and authorises the connectors they want to use.

## Enable a tentacle

1. Open `tentacles.yaml` in your [config home](../installation/configuration.md).
2. Add the block from the integration's guide under the existing `tentacles:` key.
   Give it a stable id and supply its required settings and credentials in YAML
   or environment variables. `.env` is optional.
3. Set `enabled: true` if the setup generator left it disabled. Omitted `enabled`
   defaults to `true`. Bind each channel to the ids of enabled agents.
4. [Check the configuration](../installation/configuration.md#validate-without-starting)
   and restart Octomate. For a managed Mac, use `octomate service restart`.
5. Send a message through the channel and check the reply. For native sessions,
   complete the agent's [hooks and MCP setup](../installation/clients/quickstart.md).

For example, this enables Claude Code and makes it the entry agent in Trunkline:

```yaml
tentacles:
  claude:
    type: claude
    enabled: true
    permission_mode: default
  trunkline:
    type: trunkline
    enabled: true
    agents: [claude]
```

This is the tentacle registry only. Trunkline also needs
[sign-in and a frontend build](../usage/channels/trunkline.md#configure).
The first id in a channel's `agents` list answers new conversations; later entries
are additional routes. See the [registry rules](../installation/tentacles.md#registry-rules)
before adding more instances.
