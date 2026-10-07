# DeepSeek Harness

!!! warning "Experimental"
    The `deepseek` tentacle is work in progress. It drives and records sessions, but
    several things the other harnesses have are missing, listed at the end.

`type: deepseek` drives DeepSeek Harness (`dsh`) the way dsh's own web client does:
over its `/api` gateway, HTTP for calls and a multiplexed WebSocket for events.
Octomate always starts and owns its own `dsh web` child; it never attaches to a
running one.

## Enable

Install `dsh` and configure its models and credentials as the server account.
Add the block below to `tentacles.yaml`, set `executable` if `dsh` is not on the
service's `PATH`, and bind `deepseek` in a channel's `agents` list.
[Check and restart Octomate](../../tentacles/index.md#enable-a-tentacle), then
verify a reply. For native collection, follow
[DeepSeek Harness client setup](../../installation/clients/deepseek.md).

```yaml
tentacles:
  deepseek:
    type: deepseek
    executable: dsh              # on PATH, or an absolute path to a built dsh
    port: 3081                   # the child's port; native dsh keeps 3080
    dsh_home: ~/.dsh             # settings, credentials, sessions shared from here
    permission_mode: workspace-write
    agent_preset: ~
    extra_args: []               # placed before --host/--port, e.g. --patch <overlay>
    browser_url: ~               # a reverse proxy's origin, for the printed login link
    approval_timeout: 3600
    ready_timeout: 60
```

The child runs with a fresh temporary `DSH_HOME`. Only four things are shared back
from `dsh_home`: `settings.yaml`, `.credentials.yaml`, the sessions directory and
attachments. Its plugins, hooks, MCP servers and profile patches are not loaded;
add anything the child needs through `extra_args`.

Startup reads the model catalog and the permission preset catalog from the harness,
so custom presets work, and a configured `permission_mode` the harness does not
know fails the start. The child's launch token is exchanged for a cookie on
loopback; the login link the child prints is logged once for browser access.

Choose effort from the model's advertised names. DSH passes provider-defined
names unchanged, including plugin-specific levels.

## Driven runs

You can change a driven conversation's [permission mode](permissions.md) while
it is running. Ordinary prompts and commands use the selected model and permissions.

A session is created with the thread's [workspace](../workspaces.md) as its
working directory, and that is fixed for the session's life. Model and effort are
selected before each prompt, since dsh has no per-turn override, and the posture is
set through the `/permission` command. Run-level instructions are prepended to the
prompt text; dsh has no separate instructions channel.

After each driven turn, Octomate reads dsh's current session title and stores it as
the session name and thread title. Child conversations keep their own names without
renaming the parent thread. Missing or blank titles leave the existing name intact;
a failed title lookup does not discard the turn's result.
If dsh finishes generating a title after that lookup, it is collected after a later
turn.

Approvals and questions arrive on the event socket and are answered through the
gateway. Questions are matched back by option label, and a multi-select question
carries several picks. A commissioned run with no user rejects approvals and
cancels questions at once.

## Native sessions

`octomate deepseek hooks install --bridge <checkout>/packages/hooks/hooks-claude-code`
links the bridge plugin that speaks Claude Code's hook dialect and mounts it through
a patch row. Only `UserPromptSubmit` and `Stop` fire, and the bridge carries no
per-turn key and no answer, so the hooks only mark a session live and a turn's
end. The tail does the rest: it polls the local dsh gateway, since the log is
compressed in frames that only advance at checkpoints, and ships each event with
its sequence number where a byte offset would go. Turns therefore appear when they
close, not token by token.

The tail also collects dsh's `session/title` events, including revisions and titles
that arrive outside a turn, and updates the native session name and thread title.

Resuming a driven session natively skips already recorded driven turns when their
runtime identity is available. See [switching sessions](sessions.md#what-to-expect-when-switching)
for where new turns land and the limits for older history.

A trailing interrupted turn is withheld until a later event proves its closing
records are real, because the gateway synthesises closers for a still-open turn.
Subagent child sessions are skipped by the tail. Hooks and the tail need dsh's
launch token: see [Hooks and MCP](../../installation/clients/deepseek.md).

## Runtime commands

Command discovery reads the live registry for the conversation's native DSH
session. It returns each command's name, description and input hint, and preserves
the effective definition's plugin identity. Scoped
overrides come from DSH; Octomate keeps no command-name inventory.

Discovery requires an existing native session and a running Remote connection.
It creates no session and submits no prompt. Catalogs refresh after registry-change
notifications, connection startup or shutdown, and changes to the conversation's
native session. Repeated registry notifications invalidate the cache; the next
inspection reloads it. Explicit refresh also reloads the registry.

The contract is checked against `@deepseek-ai/dsh-commands` 0.1.7-rc.1 at
[revision 46a7f68](https://github.com/deepseek-ai/deepseek-harness/blob/46a7f68b0922371ce7144b668b90e377d8e799f4/packages/interaction/commands/src/types.ts).
The internal permission command uses Ink's typed execution result. A missing
command, rejected preset or malformed result stops the run before prompting.

Explicit commands execute through `commands/execute`, preserving raw arguments
and native result text. Direct controls return their result without creating a
model turn. Commands such as `/plan <message>` stream their immediate native turn
through the existing Reflex command entry, including approvals, recording and
channel delivery. The command's feedback stays out of model history; only the
native run's actual input and output enter that history. Duplicate deliveries do
not execute the command again.

Octomate subscribes before dispatch and waits for the matching `command/done`
record, plus the end of any turn opened by that handler. It then releases the
subscription and workspace. Native permission-change events update the stored
conversation posture, so the next ordinary run keeps the new setting.

All `/goal` operations are excluded from discovery and execution. Independently
scheduled work after a command finishes is outside this command lifecycle.
Command attachments are unavailable and rejected before dispatch. Client-owned
log export is shown disabled, directing the user to DSH's web UI. Compaction runs in DSH;
this proxy does not introduce a second compaction-history model.

| Command, when advertised | Handling |
|---|---|
| `/compact` | DSH owns compaction; the command result reports its outcome. |
| `/plan off` | Native control with direct feedback. |
| `/plan <message>` | Native command followed by its immediate recorded agent run. |
| `/permission` | Native control; permission events update Octomate's saved posture. |
| Session log export | Disabled for DSH's export plugin; use its web UI. A different plugin with the same display name keeps its own behavior. |
| `/goal` | Omitted; independently scheduled continuation is not integrated. |
| Other registry commands | Native execution with direct feedback or an immediate recorded run. |

## Not yet

- **No Octomate tools in a driven run.** No MCP server is mounted and no routing
  spells are offered, so a dsh turn cannot bind a project or hand off. Bind the
  thread from Trunkline first.
- **No resume from a deferral and no teleport.** The run parameters exist but the
  tentacle does not act on them.
- **No structured output**; dsh's prompt call has no output schema.
- **No reconnect.** A dropped event socket fails in-flight runs after saving what
  arrived. Restart Octomate to recover.
- **An orphaned child.** A SIGKILLed Octomate leaves its `dsh web` child running;
  stop it by hand or the next start fails on the occupied port.
- **Text-only prompts**, and a version check that only warns. The protocol
  handshakes are the real gate.
