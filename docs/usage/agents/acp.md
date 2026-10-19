# ACP

!!! note "WIP"
    ACP support in Octomate is still in development. The features under
    [Coming soon](#coming-soon) are planned and are not available yet.

Use ACP to run Claude or Codex through Octomate. Each connection owns its
native sessions, receives streaming updates and answers the agent's permission
requests. Work runs as the Octomate service account.

## Connect

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

No agent or channel tentacle block is needed for this API. Each connection starts
its own adapter when it initializes.

## Session settings

Your client receives the modes and configuration options the provider offers.
Change them on a session created through the current connection, including while
a prompt is running. Available choices depend on the provider; it may reject a
change or update the available options as work proceeds.

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

## Current limits

Disconnecting shuts down the adapter. You cannot reconnect to drive that session
through this API yet; its provider session and registered identity are retained.
Session loading and listing are not available.
API-key revocation and expiry are checked when connecting.

## Coming soon

- Reconnect to existing sessions, resume work, and load or list sessions.
- Run Inkling through ACP using Pydantic AI.

DeepSeek Harness support is planned after Claude, Codex and Inkling.
