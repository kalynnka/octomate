# The tentacle registry

Agents, channels and MCP connectors are all **tentacles**, declared in one map keyed
by an id you choose. `type` selects the implementation.

```yaml
tentacles:
  claude:
    type: claude
  reviewer:
    type: codex
  slack:
    type: slack
    app_id: A0123456789
    agents: [claude, reviewer]
  console:
    type: trunkline
    agents: [claude]
  linear:
    type: oauth_discovery
    url: https://mcp.linear.app/mcp
```

| Kind | `type` values | Reference |
|---|---|---|
| Agent | `claude`, `codex`, `deepseek`, `inkling` | [Agents](../usage/agents/index.md) |
| Channel | `slack`, `lark`, `discord`, `napcat`, `trunkline` | [Channels](../usage/channels/index.md) |
| MCP | `bare`, `oauth`, `oauth_discovery` | [MCP proxy](../usage/mcp/proxy.md) |

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
  block, and its routes are still validated.

The key is the tentacle's identity everywhere downstream. A stored profile names the
channel it came from by this key, a thread records it as its origin, and MCP
installations are recorded against it. Renaming a key re-homes its threads, so pick
one and keep it.

Unknown keys are dropped, not rejected, at every level. The older `agents:`,
`channels:` and `mcp:` top-level blocks are unknown keys today, so a home that still
uses them boots with no tentacles at all. Two things are refused outright: a
tentacle block with no `type`, and a Codex block with the obsolete `sandbox` key,
which is now part of `permission_mode`.
