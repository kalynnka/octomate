# ACP

!!! note "WIP"
    ACP support in Octomate is still in development. The features under
    [Coming soon](#coming-soon) are planned and are not available yet.

Use ACP to run Claude or Codex through Octomate. Each connection owns its
native sessions, receives streaming updates and answers the agent's permission
requests. Work runs as the Octomate service account.

## Connect

Add ACP to your existing `tentacles:` map in `config/tentacles.yaml`, then restart
Octomate:

```yaml
tentacles:
  acp:
    type: acp
```

The registry key can be any unused tentacle id. Declare ACP once per deployment.
Omitting the block or setting `enabled: false` leaves the ACP API unavailable.
ACP registers one agent under that key. Claude and Codex are provider choices
within ACP; their separate SDK tentacles can be enabled independently.

1. Install the [Claude](https://github.com/agentclientprotocol/claude-agent-acp)
   or [Codex](https://github.com/agentclientprotocol/codex-acp) adapter for the
   service account. Its executable must be on the service's PATH. Complete the
   provider's login there before connecting; Octomate does not install adapters
   or expose provider login through this connection.
2. In Trunkline's **Account** panel, issue a key with the `acp` scope. Save the
   token when it is shown. See [Accounts and tokens](../../installation/accounts.md).
3. Connect using the [WebSocket contract](../../api/tentacles/acp.md#octomate.tentacles.acp.routes),
   initialize, and create a session. Use WSS for a remote server. The client must
   support Octomate's custom transport and bearer header; a client that only
   launches stdio agents cannot connect directly.
4. Send a prompt. Respond to permission requests in your client, or cancel
   the prompt. Keep the connection open for subsequent turns.

Separate Claude or Codex tentacle blocks are not needed for this API. Each
connection starts its own adapter when it initializes.

Each prompt runs through the [reflex graph](../../concepts/reflex.md). A session
accepts one turn at a time and becomes idle again when the turn and its history
recording finish. Keeping a session connected does not keep it busy.

## Session settings

Your client receives the modes and configuration options the provider offers.
Change them on a session created or reopened through the current connection,
including while a prompt is running. Available choices depend on the provider;
it may reject a change or update the available options as work proceeds.

## Where work happens

Choose a directory accessible on the server. Octomate preserves that directory
and creates no managed checkout. Client-supplied stdio MCP commands also run on
the server. File and terminal callbacks reach your client when the adapter
uses its advertised capabilities; these callbacks do not mount a remote directory.

## History

Creating a session writes its native thread and conversation identity to Octomate.
Your first prompt makes it visible in your Trunkline history. Octomate records
each prompt and the replies, thinking and tool activity received through ACP,
and publishes live updates for viewers of that thread. No hook setup is needed.
Cancelled or failed prompts retain the input and any output already received.

This history contains what the provider shares through ACP. It does not include
hidden model messages or infer model usage. Media remains in the recorded
conversation; the thread ledger shows a media label.

## Reopen a session

Disconnecting shuts down the adapter and keeps the session's history. Connect
again as the same Octomate user, initialize, and list your available sessions.
Load a session to replay its history into your client, or resume it without
replay. Then continue prompting. Availability depends on the provider's
advertised capabilities; see the [API contract](../../api/tentacles/acp.md#octomate.tentacles.acp.routes).

You can reopen native sessions whose history you can access in Octomate.
An empty session disconnected before its first prompt has no recorded
participant and cannot be reopened through this API. A session can attach to
only one ACP connection on this Octomate instance at a time; disconnect the
previous client before reopening it elsewhere.

Replaying history does not create duplicate messages or runs in Octomate.

## Current limits

API-key revocation and expiry are checked when connecting.

## Coming soon

- Run Inkling through ACP using Pydantic AI.

DeepSeek Harness support is planned after Claude, Codex and Inkling.
