# Channels

A channel tentacle holds one platform connection open, translates its events into
messages, and draws a run back onto the platform in whatever that platform can show.
Keyed by instance, so two Lark apps are two keys, two separate sets of threads, and
two bots.

## Enable a channel

Follow the app setup for [Slack](slack.md), [Lark / Feishu](lark.md) or
[Discord](discord.md), or build [Trunkline](trunkline.md) for browser access.
Add its block to `tentacles.yaml`, supply the required credentials, and name
at least one enabled agent in `agents`. Set `enabled: true` if the generator
left it disabled. Check the configuration, restart Octomate and send a message
to verify the connection. See the [shared enablement steps](../../tentacles/index.md#enable-a-tentacle).

```yaml
tentacles:
  slack:
    type: slack
    app_id: A0123456789
    agents: [inkling, claude]
    mention_only: true
    stream:
      flush_interval: 0.2
    recap:
      messages: 16
      characters: 1000
```

Supply secrets in YAML or environment variables named
`OCTOMATE__TENTACLES__<KEY>__<FIELD>`. A `.env` file is optional; remove matching
YAML placeholders if using it, because YAML takes precedence.

## Shared settings

| Key | Default | Meaning |
|---|---|---|
| `agents` | required | Agent ids, entry order. The first answers new conversations; every one is a route. |
| `enabled` | `true` | A disabled channel keeps its block and its routes are still validated. |
| `mention_only` | `true` | On a surface others can read, the bot answers only when addressed. |
| `stream` | per platform | Live rendering: `enabled`, `flush_interval`, `min_chars`, `max_chars`, `fold_threshold`. |
| `recap` | 16 messages, 1000 chars each | How much of a chat room a kick is shown, since a kick there starts with an empty context. |
| `mcp` | `false` | Whether this channel serves its own MCP tools to the agents on it. Slack only. |

Streaming defaults differ: Slack, Lark and Trunkline stream by default; Discord
coalesces edits and is off until you set `stream.enabled: true`.
Within a `stream:` block you set only what you change and keep the
platform's other defaults.

## Being addressed

The mention gate looks at whether the surface is **shared**, not at its type. A
direct message, a Slack assistant pane, or a Lark one-to-one chat is never gated.
On a shared surface the bot answers when:

- the message `@`-mentions it,
- the message replies to one of its messages, which Discord reports and Slack and
  Lark do not, or
- the message lands in a thread an agent already owns, because a handoff pinned it.

A group's main channel is never pinned to an owner, so there a mention stays the
rule even after a long exchange. Set `mention_only: false` to answer everything on
that channel.

## Identity

The first time someone speaks, the channel records a **visitor profile**: a platform
identity owned by nobody. The agent still answers. What a visitor lacks is
everything that follows the person across channels: history search, summons into
their direct messages elsewhere, their MCP connectors. Linking the profile to an
account fixes that; see [Accounts and tokens](../../installation/accounts.md#link-
your-channel-profiles).

## What each channel renders

| | Slack | Lark | Discord | Trunkline |
|---|---|---|---|---|
| Streaming text | Native stream | Streaming card | Message edits | Every event, live |
| Thinking and tool cards | Folding plan | Cards that fold | No | Yes |
| Todo checklist | Plan tasks | Card | No | Yes |
| Approval cards | Buttons, paged | One card each | Buttons, one each | Yes |
| Question cards | Wizard | Card, paged | Buttons and a modal | Yes |
| Sub-threads | Yes | Yes | Public threads from a text channel | No |
| Direct messages | Yes | Yes | Yes | No |
| Profile linking | From chat, or Slack OAuth | From chat | From chat, or Discord OAuth | Not needed |

Subagents get a timeline of their own on Slack, Lark and Discord. Every channel
posts a one-line error with a trace id when handling a message fails, and ignores a
message the platform delivers twice.

The [threads page](../threads.md) explains what each platform's chats and threads
become inside Octomate, and where a reply lands.
