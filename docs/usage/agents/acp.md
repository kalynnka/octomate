# ACP

!!! note "WIP"
    ACP support is still in development. The features under
    [Coming soon](#coming-soon) are not available yet.

Use ACP to work with Octomate's enabled agents. Your client connects to one
Octomate agent and chooses a model from the available options. Changing that
choice keeps the same session and thread history.

## Connect

Add ACP to your existing `tentacles:` map in `config/tentacles.yaml`, then restart
Octomate:

```yaml
tentacles:
  acp:
    type: acp
```

Declare ACP once per deployment, under any unused registry key. Omitting it or
setting `enabled: false` leaves the API unavailable.

1. Enable and authenticate the agent tentacles you want to use, such as
   [Claude](claude-code.md), [Codex](codex.md), or [Inkling](inkling.md). ACP uses their
   existing runtimes and model catalogs; no separate provider adapter is needed.
2. In Trunkline's **Account** panel, issue a key with the `acp` scope. Save the
   token when it is shown. See [Accounts and tokens](../../installation/accounts.md).
3. Connect using the [WebSocket contract](../../api/tentacles/acp.md#octomate.tentacles.acp.routes),
   initialize, and create a session. Use WSS for a remote server. The client must
   support this custom transport and its bearer header; stdio-only clients need
   a transport bridge.
4. Choose an advertised model, then send a prompt. Answer permission requests in
   your client, or cancel the prompt. Questions require a client that supports
   form elicitation.

Each prompt runs through the [reflex graph](../../concepts/reflex.md). A session
accepts one prompt at a time. Keeping it connected does not keep the thread busy.

## Session settings

The model selector contains choices from the enabled agent tentacles.
Its default uses the first available agent's own default. Permission choices belong to the
selected agent and change when you choose a different agent.

You can change the model during a prompt. The current prompt, including its
approval or question continuations, finishes with its original choice; the next
prompt uses the new choice. Each agent retains its own conversation within the
session's thread. Switching agents does not copy another agent's private model
history.

## Where work happens

File operations and terminal commands run on the Octomate server. A directory in
a declared project selects that project's managed checkout. An unregistered
directory selects an empty chat workspace, discarded when the prompt ends.
Project work follows the normal [workspace lifecycle](../../concepts/workspaces.md).
Symbolic-link aliases work when selecting or filtering a project.

Octomate's tools are available when enabled for the selected agent, using your
account's permissions and linked MCP accounts.

MCP servers supplied by the client are rejected when creating or reopening a
session. Client file operations and terminals are not supported.

## History and reopening

A session is an Octomate thread. Its conversations, runs, and messages belong to
the agents that execute them. The first prompt makes the thread visible in your
Trunkline history. Live activity is published through the nerve; no hooks are
needed.

Disconnecting cancels active work and retains history. Connect again as the same
Octomate user to list and reopen your sessions, including empty sessions. Load
replays the thread's messages; resume continues without replay. Replaying does
not create duplicate messages or runs. Only one connection can attach a session
at a time.

Reopening keeps the selected model and the thread's project. Create a new session
to choose a different project. Sessions from the earlier provider-specific API
retain their recorded history but cannot be resumed through this endpoint.

## Current limits

API-key revocation and expiry are checked when connecting. Media prompts are not
advertised. History replay presents the thread ledger; detailed tool and thinking
history remains in Octomate's conversations.

## Coming soon

- External ACP providers as configurable agent backends.
- File operations and terminal commands in the connected client's environment.
- MCP servers supplied by the client.
