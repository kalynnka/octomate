# Tentacles

A **tentacle** is one connection Octomate holds open: to an agent runtime, to a
chat platform, or to an MCP server. Every tentacle is declared in `tentacles.yaml`
under an id you choose, with `type` selecting the implementation, and that id is how
the rest of the system names it. See [Configuration](../installation/tentacles.md)
for the registry rules.

## Agents

An agent tentacle wraps a harness somebody else built. Octomate drives it; it does
not reimplement it. Three of the four also record the sessions you run yourself.

| Agent | Runtime | Records native sessions | Status |
|---|---|---|---|
| [Claude Code](agents/claude-code.md) | Claude Agent SDK, local subprocess | Hooks and transcript tail | Ready |
| [Codex](agents/codex.md) | openai-codex app-server, pooled per conversation | Hooks and rollout tail | Ready |
| [DeepSeek Harness](agents/deepseek.md) | A `dsh web` child Octomate owns | Hooks and gateway tail | Experimental |
| [Inkling](agents/inkling.md) | In-process Pydantic AI | No | Ready. The fallback for any model Pydantic AI supports |

[Agents](agents/index.md) covers what they share: routes and claims, models,
permission modes, and how approvals reach you.

## Channels

A channel tentacle is where you talk. It turns platform events into messages,
renders a run natively on that platform, and carries approvals and questions back.

| Channel | Transport | Status |
|---|---|---|
| [Slack](channels/slack.md) | Bolt over Socket Mode | Ready |
| [Lark / Feishu](channels/lark.md) | lark-oapi long connection | Ready |
| [Discord](channels/discord.md) | discord.py Gateway | Ready |
| [Trunkline](channels/trunkline.md) | The web console, over `/api/trunkline` | Preview, changing week to week |
| [QQ through NapCat](channels/napcat.md) | OneBot WebSocket to a NapCat bridge | Unverified |

Every channel dials out, so none needs an inbound port. [Channels](channels/index.md)
covers what they share, and what each one can and cannot render.

## MCP connectors

An MCP tentacle is a vendor MCP server declared once by the operator and installed
by each user for themselves, so an agent acting for a person uses that person's
grant. Three generic types cover any server, and Slack's channel doubles as one.
See [MCP proxy](mcp/proxy.md).

## How they combine

A channel names the agents that serve it:

```yaml
tentacles:
  slack:
    type: slack
    agents: [inkling, claude]
```

The first entry answers new conversations with its default model. Every bound
agent, and every model in its catalog, is a route the answering agent may
[summon](gateway.md) for work that suits it better. Which agents serve a channel is
that channel's own business: a summon into another channel is resolved against the
destination's list, not the origin's.

## Native and driven sessions

| | Native session | Driven session |
|---|---|---|
| Starts | In your terminal or editor | From a channel message, or a summon |
| Runs | On your machine, with your login and your local config | On the server, as the service account, in a [workspace](workspaces.md) |
| Records | Through the hooks and the transcript tail | Directly, as it runs |
| Tools | Your own, plus Octomate's over MCP if installed | Octomate's only; local hooks, plugins and MCP servers are disabled |
| Readable | In Trunkline, and searchable by any agent answering you | Everywhere the thread lives |

A native session is a thread like any other. An agent answering you elsewhere can
search it, and a brief can cite a message from it by its handle.

## In this section

| Page | What it covers |
|---|---|
| [Agents](agents/index.md) | Routes, claims and models, then one page per runtime |
| [Permissions and approvals](agents/permissions.md) | Each runtime's posture, and how a card reaches you |
| [Driven and native sessions](agents/sessions.md) | What a run from a channel switches off; how your own sessions arrive |
| [Channels](channels/index.md) | Mentions, streaming, identity, and the per-platform render matrix |
| [Threads and chat rooms](threads.md) | The one model behind every surface, and how each platform maps onto it |
| [Moving a conversation](gateway.md) | summon, teleport, scheme, send, dispel, scry |
| [Approvals and questions](actions.md) | Actions, batches, and what happens when nobody answers |
| [Projects](projects.md) | Declaring a code location, and how a thread binds to it |
| [Workspaces](workspaces.md) | The checkout a driven run works in, and what is kept |
| [History](history.md) | What an agent can search, and how far it can see |
| [Octomate MCP](mcp/octomate.md) | The tools Octomate offers to any agent |
| [MCP proxy](mcp/proxy.md) | Vendor connectors installed per user |
