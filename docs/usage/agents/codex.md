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
    effort: ~                      # e.g. none | minimal | low | medium | high | xhigh | max | ultra
    summary: ~                     # auto | concise | detailed | none
    personality: ~                 # none | friendly | pragmatic
    runtime:
      # codex_bin: /opt/bin/codex
      config_overrides: []
```

The app-server supplies the catalog: every non-hidden model with its supported
reasoning efforts, keyed as `<provider>:<model>`, `openai` unless the Codex config
names another provider.

Trunkline offers the default model's effort levels before the first message,
including when you leave the model at Harness default. Existing conversations
use their selected or last reported model's levels.

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

The command finder lists Codex's app and IDE commands even before the first
message. The matrix below covers built-in commands that need special handling or
are unavailable. **Disabled** means the command is visible but cannot execute;
**omitted** means it is not listed. Planned handling is not available yet.

| Command | Available now | Handling or alternative |
|---|---|---|
| `/approve` | Disabled | Planned: approve a selected recent automatic-review denial for a retry. Any retry run must be recorded and delivered through Octomate. |
| `/compact` | Available after a driven run | Compact the existing Codex context. Reports success only after compaction finishes; keeps the visible conversation history. Takes no arguments. |
| `/plan [on\|off]` | Available after a driven run | Enter Codex planning mode, or leave it with `off`. Keeps the selected model and effort. Changing mode starts no run; the next prompt uses the mode. |
| `/review [branch]` | Available in a conversation | Run Codex's built-in review of uncommitted changes, or changes against the named branch, in the conversation's workspace. Streams and records the review like other agent runs. |
| `/init` | Available in a conversation | Ask Codex to create or update `AGENTS.md` in the conversation's workspace through a recorded agent run. This can write files and follows the conversation's approvals. |
| `/status` | Available, including before a conversation exists | Show Octomate's conversation, workspace, model, effort and permission selections. Does not start a run. Native token usage and account rate limits are not included yet. |
| `/mcp` | Available after a driven run | List the driven conversation's MCP servers, tool and resource counts, and authentication status. Starts no run. |
| `/reasoning [level]` | Available in a conversation | Save the same effort selection as `/effort`, validated against the selected model. Omit the level to restore the default. Use `/effort` before the first message. |
| `/fork` | Disabled | Planned: create an independent Octomate thread and conversation with copied history and settings, preserving the source. |
| `/model` | Disabled | Use Trunkline's agent/model picker when starting a conversation. Planned: open that shared control from the command. |
| `/project` | Disabled | Use Trunkline's project selector. Planned: open the same selector from the command. |
| `/task` | Disabled | Use `/new` and leave the project unselected. Planned: start a conversation through Octomate's new-thread flow. |
| `/worktree` | Disabled | Workspace creation belongs to Octomate's [workspace lifecycle](../workspaces.md). Selecting a new worktree through this command is planned. |
| `/local` | Disabled | Driven runs already use the conversation's local workspace. Execution location is managed by Octomate. |
| `/fast` | Disabled | Planned: offer fast execution only when the selected model and runtime support it. |
| `/memories` | Disabled | Planned: expose memory controls only when supported by the runtime. |
| `/personality` | Disabled | Planned: expose response-style controls only when supported by the model and runtime. |
| `/cloud` | Disabled | Cloud execution is not integrated. |
| `/cloud-environment` | Disabled | Cloud environment selection is not integrated. |
| `/ide-context` | Disabled | Automatic editor context belongs to the Codex IDE extension. |
| `/feedback` | Disabled | Use Codex's feedback dialog. |
| `/pet` | Disabled | Manage desktop pets in the Codex app. |
| `/side` | Disabled | Temporary side conversations are not supported here. |
| `/goal` | Omitted | Goal execution and automatic continuation are deferred. |
| CLI-only commands | Omitted | This catalog covers app and IDE commands. |

This matrix concerns driven conversations. Synced native sessions remain
read-only; use an available [gateway operation](../channels/trunkline.md) to
continue elsewhere.

Planning, compaction and MCP inspection require the conversation to be loaded in the current
Codex runtime. After a runtime restart, send a message to resume it first.

Enabled skills appear alongside those actions once a conversation workspace
exists. Inspection does not create a workspace, start a Codex thread or send a
prompt. Skills keep their native names and descriptions; a skill sharing a name
with a disabled built-in remains selectable.

The host caches the catalog until explicit refresh, context changes or eviction.
A `skills/changed` notification or runtime disconnect invalidates the agent's
catalogs. Preparing a driven turn invalidates that conversation's catalog as well.
Refresh rescans Codex's own skills cache; use it for changes the runtime has not
reported. Skill-loading errors appear alongside the known app commands so the
finder explains why skills are missing.

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

- **Structured output** rides the turn's output schema; there is no retry loop.
- **Images in a prompt** are dropped.
