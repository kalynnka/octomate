# DeepSeek Harness

!!! warning "Experimental"
    The `deepseek` tentacle is work in progress. It drives and records sessions, but
    several things the other harnesses have are missing, listed at the end.

`type: deepseek` drives DeepSeek Harness (`dsh`) the way dsh's own web client does:
over its `/api` gateway, HTTP for calls and a multiplexed WebSocket for events.
Octomate always starts and owns its own `dsh web` child; it never attaches to a
running one.

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

Effort is mapped onto dsh's adapter vocabulary. An Octomate level maps to itself
when dsh advertises that id, and otherwise through `efforts`, whose default suits
`llm-deepseek`: `minimal` and `low` to `off`, `medium` and `high` to `high`, `xhigh`
to `max`.

## Driven runs

A session is created with the thread's [workspace](../workspaces.md) as its
working directory, and that is fixed for the session's life. Model and effort are
selected before each prompt, since dsh has no per-turn override, and the posture is
set through the `/permission` command. Run-level instructions are prepended to the
prompt text; dsh has no separate instructions channel.

Approvals and questions arrive on the event socket and are answered through the
gateway. Questions are matched back by option label, so a multi-select question
can carry one selection from Octomate. A commissioned run with no user rejects
approvals and cancels questions at once.

## Native sessions

`octomate deepseek hooks install --bridge <checkout>/packages/hooks/hooks-claude-code`
links the bridge plugin that speaks Claude Code's hook dialect and mounts it through
a patch row. Only `UserPromptSubmit` and `Stop` fire, and the bridge carries no
per-turn key and no answer, so the hooks only mark a session live and a turn's
end. The tail does the rest: it polls the local dsh gateway, since the log is
compressed in frames that only advance at checkpoints, and ships each event with
its sequence number where a byte offset would go. Turns therefore appear when they
close, not token by token.

A trailing interrupted turn is withheld until a later event proves its closing
records are real, because the gateway synthesises closers for a still-open turn.
Subagent child sessions are skipped by the tail. Hooks and the tail need dsh's
launch token: see [Hooks and MCP](../../installation/clients/deepseek.md).

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
