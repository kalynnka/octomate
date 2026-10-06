# Codex

`type: codex` drives Codex through one shared openai-codex SDK app-server per
tentacle and records the Codex sessions you run yourself.

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
    runtime:
      # codex_bin: /opt/bin/codex
      config_overrides: []
```

The app-server supplies the catalog: every non-hidden model with its supported
reasoning efforts, keyed as `<provider>:<model>`, `openai` unless the Codex config
names another provider.

Octomate loads this catalog at startup using the SDK's bundled Codex runtime,
unless `runtime.codex_bin` selects another executable. Updating a separate Codex
CLI or IDE extension does not update the bundled runtime. Restart Octomate after
upgrading its Codex dependency to refresh the available models.

## Driven runs

One app-server process handles model discovery, skill discovery and all driven
conversations. Octomate saves a new native thread ID before starting its first turn.
Before each later turn, it loads the conversation's native ID from the database,
asks Codex whether that thread is loaded, and resumes it when needed. The applied
credential and conversation header are tracked in memory for the life of the
native thread; tokens also remain in memory. Moving the native handle to another
conversation causes the next turn to reapply the destination's credential and header.
The process starts when the tentacle starts and closes when it stops. If the
process disconnects, requests fail until Octomate restarts.

After each turn, Octomate reads the Codex thread's name through the SDK without
loading its turn history. When Codex supplies a nonblank name, Octomate updates
the conversation name and its thread title. Later turns pick up revised names;
subagent conversation names do not replace the parent thread's title. A failed
name lookup is logged and does not discard the run's result.

The run's working directory is the thread's [workspace](../workspaces.md). In
`user_review` and `auto_review` the sandbox is `workspace_write`, so that directory
is the write boundary; `full_access` removes the sandbox and the prompts. Both the
sandbox and the approval policy are reapplied on every turn. Changing a mode also
sends the new settings to the loaded Codex thread immediately, without sending a
prompt or interrupting work. Codex retains an active turn's permissions; the new
mode takes effect on the next run. A rejected update leaves the saved selection
unchanged.

Local customisation is off through config overrides appended after your own:
hooks, plugins, apps and notifications are disabled, and every MCP server in the
local Codex config is switched off for the thread. One server is added instead,
`octomate_driven`, pointing at Octomate over HTTP with a temporary `mcp`-scoped API
key shared by that user's conversations within the tentacle. Each thread receives
the user's credential and its own conversation header. When the asking user changes
or the credential expires, Octomate unsubscribes from that idle thread and resumes
it with the current user's credential. Codex reloads the unsubscribed idle thread
to apply the changed configuration; Octomate preserves the server's default idle
unload delay. Changing credentials while the native thread is active is refused.
Expired keys are replaced once per user; other loaded threads adopt the
replacement on their next turn. Unloading a thread preserves
the shared key for the user's other conversations. Remaining credentials are
revoked when the tentacle shuts down. Codex lists those tools
under `mcp__octomate_driven`. Your `developer_instructions` and `base_instructions`
are carried on start and on resume.

## Runtime commands

The command catalog discovers enabled skills through Codex's `skills/list` API.
It preserves native names and descriptions, and identifies skills by their paths,
so two skills with the same name remain distinct. Codex does not expose a general
CLI slash-command catalog through this API.

Discovery requires an existing conversation workspace. Before one exists, the
catalog reports unavailable; inspection does not create a workspace, start a
Codex thread or send a prompt. It uses the tentacle's shared app-server, including
its configuration overrides and change notifications.

The host caches the catalog until explicit refresh, context changes or eviction.
A `skills/changed` notification or runtime disconnect invalidates the agent's
catalogs. Preparing a driven turn invalidates that conversation's catalog as well.
Refresh rescans Codex's own skills cache; use it for changes the runtime has not
reported. Native skill-loading errors appear in catalog limitations. If errors
leave no enabled skills, discovery reports failed instead of an empty success.

Explicit skill execution refreshes discovery for the selected conversation and
resolves the skill from that catalog. Disabled, removed or out-of-scope skills are
rejected. Codex receives a typed skill reference and the raw argument text, then
runs through the same streaming, approval and history path as a driven turn.
Arguments may be empty; attachments are not supported. A skill's path comes from
discovery, never from a separate client-supplied path.

## Approvals and questions

Codex's own approval requests, command execution and file changes, and its
elicitations become Octomate actions. A denied request tells Codex why. Its
request to run an MCP tool becomes an approval card in Codex's own words, showing
the arguments the call would run with: approving lets the call run, and declining
refuses it. Octomate's tools that only read, inspecting where a conversation can
go and reading history, say so, and Codex runs those without asking. Choices
Codex offers, such as MCP consent prompts, are presented as they are. Under
`auto_review` and `full_access` no request reaches the bridge at all. As with
Claude, the wait is in process, so an answer is not durable across a restart.
Requests are routed by native thread ID and answered asynchronously. One person's
approval wait does not block the shared reader from delivering another
conversation's events or answering discovery requests.

The gateway's [teleport tool](../gateway.md#teleport) is configured with Codex's
native `prompt` approval mode. It follows the selected permission policy and
reviewer, including `auto_review`; Octomate does not add another confirmation.

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

Teleporting a native session uses its latest fully uploaded completed
or aborted turn. A session can also teleport itself: Codex names its thread on
every MCP call, which is how the served teleport finds the session's history. Later turns stay in the source session, including any turn still
in progress. The new conversation keeps the selected turn's model, permissions
and an independent transcript snapshot.

Resuming a driven session natively skips already recorded driven turns when their
runtime identity is available. See [switching sessions](sessions.md#what-to-expect-when-switching)
for where new turns land and the limits for older history.

## Not yet

- **Native CLI slash commands** have no discovery or execution API in this adapter.
- **Structured output** rides the turn's output schema; there is no retry loop.
- **Images in a prompt** are dropped.
