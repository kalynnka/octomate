# Codex

`type: codex` drives Codex through the openai-codex SDK's app-server, one warm
process per conversation, and records the Codex sessions you run yourself.

## Enable

Run Codex once as the server account and complete its login. Add the block below
to `tentacles.yaml`, bind `codex` in a channel's `agents` list, and
[check and restart Octomate](../../tentacles/index.md#enable-a-tentacle).
For your own native sessions, follow [Codex client setup](../../installation/clients/codex.md).

```yaml
tentacles:
  codex:
    type: codex
    permission_mode: user_review   # user_review | auto_review | full_access
    approval_timeout: 3600
    effort: ~                      # none | minimal | low | medium | high | xhigh
    summary: ~                     # auto | concise | detailed | none
    personality: ~                 # none | friendly | pragmatic
    max_clients: 8                 # warm app-servers kept; ~ = unbounded
    client_idle_ttl: 600           # seconds an idle one lives; ~ = forever
    runtime:
      # codex_bin: /opt/bin/codex
      config_overrides: []
```

The app-server supplies the catalog: every non-hidden model with its supported
reasoning efforts, keyed as `<provider>:<model>`, `openai` unless the Codex config
names another provider.

## Driven runs

Each conversation gets its own app-server process from a pool, evicted after
`client_idle_ttl` idle seconds or when the pool exceeds `max_clients`, least
recently used first. The Codex thread id is the conversation's resumable handle,
so a turn on a fresh process resumes the same thread.

After each turn, Octomate reads the Codex thread's name through the SDK without
loading its turn history. When Codex supplies a nonblank name, Octomate updates
the conversation name and its thread title. Later turns pick up revised names;
subagent conversation names do not replace the parent thread's title. A failed
name lookup is logged and does not discard the run's result.

The run's working directory is the thread's [workspace](../workspaces.md). In
`user_review` and `auto_review` the sandbox is `workspace_write`, so that directory
is the write boundary; `full_access` removes the sandbox and the prompts. Both the
sandbox and the approval policy are reapplied on every turn, so a posture change
takes effect on the next message even on a warm process.

Local customisation is off through config overrides appended after your own:
hooks, plugins, apps and notifications are disabled, and every MCP server in the
local Codex config is switched off for the thread. One server is added instead,
`octomate_driven`, pointing at Octomate over HTTP with a temporary `mcp`-scoped API
key minted for the asking user and revoked when the process closes. Codex lists
those tools under `mcp__octomate_driven`. Your `developer_instructions` and
`base_instructions` are carried on start and on resume.

## Approvals and questions

Codex's own approval requests, command execution and file changes, and its
elicitations become Octomate actions. A denied request tells Codex why. Choices
Codex offers, such as MCP consent prompts, are presented as they are. Under
`auto_review` and `full_access` no request reaches the bridge at all. As with
Claude, the wait is in process, so an answer is not durable across a restart.

## Native sessions

`octomate codex hooks install` registers `SessionStart`, `UserPromptSubmit`,
`Stop`, `SubagentStart` and `SubagentStop`. Codex must trust the hooks: open
`/hooks` in Codex after installing. The tail streams the rollout file, and child
rollouts are handed to it by the `SubagentStop` hook, since a tail cannot tell
which sibling file is its child.

Rollouts are re-streamed whole on reconnect, because the head of the file carries
the session metadata every child is classified against; committed turns are skipped
server-side, so nothing duplicates. An aborted turn is still committed as far as it
got. Codex has no session-end event, so a session ends by the tail's own idle
drain.

Resuming a driven session natively skips already recorded driven turns when their
runtime identity is available. See [switching sessions](sessions.md#what-to-expect-when-switching)
for where new turns land and the limits for older history.

## Not yet

- **Structured output** rides the turn's output schema; there is no retry loop.
- **Images in a prompt** are dropped.
