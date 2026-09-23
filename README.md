# Octomate 🐙

**Your agents. Your channels. Wherever you need them.**

Octomate is a self-hosted relay that brings your agents, conversations and tools
within reach across channels. Give an agent a task, let it work in the background,
and return to the conversation wherever you're connected. Keep using your existing
apps, editor and terminal, with the agents and logins you already have.

[Documentation](https://kalynnka.github.io/octomate/) ·
[Quickstart](https://kalynnka.github.io/octomate/installation/quickstart/) ·
[Tentacles](https://kalynnka.github.io/octomate/tentacles/)

> **Early development.** APIs and integrations are evolving. Capabilities vary by
> agent and channel; the guides describe their current limits.

## See it in action

### Start at your desk, continue elsewhere

Use your agent as usual. Octomate collects the session, so you can revisit it or
ask your agent to hand the task to a connected channel with a brief.
[Native sessions](https://kalynnka.github.io/octomate/usage/agents/sessions/)

### Think it through together, then put an agent to work

Discuss an idea in a channel, then ask: “Hand this to Codex in the website project.
Include our decisions and show me the changes before committing.”
[Handoffs](https://kalynnka.github.io/octomate/usage/gateway/#summon)

### Take the conversation with you

“Continue this in my private conversation on the other channel.” Teleport carries
the same agent and its history between supported destinations; a group discussion
can move into DMs with a brief instead.
[Moving conversations](https://kalynnka.github.io/octomate/usage/gateway/)

### Connect once, ask from anywhere

“Find the open issues in my repository and compare them with what we discussed.”
Your agent can combine recorded history with your connected GitHub tools.
[MCP connectors](https://kalynnka.github.io/octomate/tentacles/mcp/)

## Features

- **[Native and driven sessions](https://kalynnka.github.io/octomate/usage/agents/sessions/).**
  Collect sessions from supported agents, or start background work through a channel.
- **[Threads and chat rooms](https://kalynnka.github.io/octomate/usage/threads/).**
  Keep tasks independent across group conversations, private messages and the web console.
- **[Shared history](https://kalynnka.github.io/octomate/usage/history/).**
  Record conversations centrally; let agents search message text in threads you participated in.
- **[Gateway](https://kalynnka.github.io/octomate/usage/gateway/).**
  Discover destinations, hand off to another agent, teleport supported conversations,
  move a brief into DMs, or send a result without moving the task.
- **[Delegation](https://kalynnka.github.io/octomate/usage/agents/inkling/).**
  Inkling can commission another agent, follow up and bring its report back to your conversation.
- **[Native channel presentation](https://kalynnka.github.io/octomate/usage/channels/).**
  Stream replies, tool activity, todo lists, files and interactive cards as each channel supports.
- **[Approvals and questions](https://kalynnka.github.io/octomate/usage/actions/).**
  Answer individual requests in batches, from the conversation or Trunkline, with recorded decisions.
- **[Permission modes](https://kalynnka.github.io/octomate/usage/agents/permissions/).**
  Choose the runtime's supported level of oversight for a conversation.
- **[Projects and workspaces](https://kalynnka.github.io/octomate/usage/workspaces/).**
  Give project threads separate Git working copies, save changes after each turn and review diffs.
- **[Octomate MCP](https://kalynnka.github.io/octomate/usage/mcp/octomate/).**
  Bring history, messaging and gateway tools into your existing agent.
- **[MCP proxy](https://kalynnka.github.io/octomate/usage/mcp/proxy/).**
  Discover and use installed services through one connection, with personal OAuth grants
  or operator-provided credentials. Connect through Trunkline or a conversation.
- **[Accounts and identity](https://kalynnka.github.io/octomate/installation/accounts/).**
  Sign in, issue API tokens and link channel profiles to the same account.
- **[Trunkline](https://kalynnka.github.io/octomate/usage/channels/trunkline/).**
  Start conversations, browse collected sessions, answer pending requests, inspect
  workspace changes and manage MCP connections in the web console.
- **[Inkling's model and tool support](https://kalynnka.github.io/octomate/usage/agents/inkling/).**
  Use Pydantic AI models, project file and shell tools, and previews or summaries of oversized tool results.
- **[Deployment and operations](https://kalynnka.github.io/octomate/installation/).**
  CLI setup, YAML templates, hooks and MCP configuration, private networking,
  upgrades, backups, logs and optional Logfire tracing.
- **[Extensible integrations](https://kalynnka.github.io/octomate/contributing/).**
  Add agent, channel or MCP tentacles; configure multiple named instances and agent/model routes.

## Supported tentacles

### Agents

| Agent | Support |
|---|---|
| [Claude Code](https://kalynnka.github.io/octomate/usage/agents/claude-code/) | Native collection and server-driven work through the Claude Agent SDK |
| [Codex](https://kalynnka.github.io/octomate/usage/agents/codex/) | Native collection and server-driven work through Codex app-server |
| [DeepSeek Harness (`dsh`)](https://kalynnka.github.io/octomate/usage/agents/deepseek/) | Experimental native collection and driven runs; no Octomate MCP tools in driven runs yet |
| [Inkling](https://kalynnka.github.io/octomate/usage/agents/inkling/) | Built-in backup agent for any model supported by Pydantic AI; no separate harness needed |

### Channels

| Channel | Support |
|---|---|
| [Slack](https://kalynnka.github.io/octomate/usage/channels/slack/) | Channels, threads, DMs and the assistant pane |
| [Lark / Feishu](https://kalynnka.github.io/octomate/usage/channels/lark/) | Groups, threads and one-to-one chats |
| [Discord](https://kalynnka.github.io/octomate/usage/channels/discord/) | Server channels, public threads and DMs |
| [Trunkline](https://kalynnka.github.io/octomate/usage/channels/trunkline/) | Octomate's web console — preview |

### MCP connectors

| Connector | Support |
|---|---|
| [GitHub preset](https://kalynnka.github.io/octomate/tentacles/mcp/#built-in-presets) | Standard or read-only endpoint; browser and device-code OAuth |
| [Bare MCP](https://kalynnka.github.io/octomate/tentacles/mcp/#no-authentication-or-a-deployment-token) | No authentication or an operator-provided bearer token |
| [Discovered OAuth](https://kalynnka.github.io/octomate/tentacles/mcp/#discovered-oauth) | Services advertising their OAuth configuration |
| [Registered OAuth](https://kalynnka.github.io/octomate/tentacles/mcp/#a-registered-oauth-application) | Your OAuth application's credentials and supported flows |
| [Slack tools](https://kalynnka.github.io/octomate/usage/channels/slack/#mcp-tools-acting-as-the-person) | Optional tools acting as the linked Slack user |

## Get started

Give your agent the **[setup brief](https://kalynnka.github.io/octomate/installation/quickstart/#agent-tldr)**,
or choose a **[deployment guide](https://kalynnka.github.io/octomate/installation/deployment/)**.
macOS is tested end to end with live agents; Linux and Docker guides are also
available, with Docker recommended for Windows. Then
[connect your native agents](https://kalynnka.github.io/octomate/installation/clients/quickstart/).

## Contribute

Start with the [development setup](https://kalynnka.github.io/octomate/contributing/setup/),
[design concepts](https://kalynnka.github.io/octomate/concepts/) and
[API reference](https://kalynnka.github.io/octomate/api/).
The contribution guides include examples for adding each kind of tentacle.

## License

Copyright © 2026 Lu Hui. [GNU Affero General Public License v3.0](LICENSE).
