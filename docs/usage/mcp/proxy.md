# MCP proxy

Connect a service once in Octomate, then use it from your agents. Each person
chooses which connectors to install and, for OAuth services, signs in with their
own account.

## Add a connector { #installing-per-user }

1. Open **MCP** in Trunkline.
2. Choose one of the configured offerings and select **Install**, or use
   **Install MCP** to add a service by its MCP URL.
3. Give the installation a name and a distinct namespace. The namespace is a
   short label that helps distinguish connections, such as `tracker_work` and
   `tracker_personal`.
4. Complete authorisation if requested, then check that the connector is enabled
   and ready.

You can also ask your agent to help:

> Show me the available connectors and help me connect my project tracker.

If the service requires setup by the server operator, send them the
[Tentacles connector guide](../../tentacles/mcp.md). A connector being listed as
available does not mean it is already connected to your account.

For GitHub, follow the [Trunkline or conversation walkthrough](../../tentacles/mcp.md#connect-it-to-your-account)
to install the configured preset and connect your account.

## Sign in and grant access { #authorising }

Follow the authorisation prompt in Trunkline or the private link sent through your
channel. Some services ask you to enter a device code; others open a consent page.
Check which account you are signing in with and the access being requested.

Complete that step in the provider's page, then return to Octomate and check the
connection. Installing a connector alone does not finish authorisation. Keep
passwords and tokens out of the chat; use the connection form or provider's
sign-in page.

Linking your channel profile to Octomate is a separate step. If you want a
connector you installed in Trunkline to be available while chatting elsewhere,
[link that channel profile](../../installation/accounts.md#link-your-channel-profiles).

## Use it from an agent

Start with a read-only request to check access:

> Use my work tracker connection to list the tasks assigned to me.

Name the connection when you have more than one for the same service. Then ask for
changes as needed, being clear about which account or workspace to use.

These connectors are available to Inkling, driven Claude Code and Codex, and
native agents connected through [Octomate MCP](octomate.md). Driven DeepSeek
Harness cannot use them yet.

OAuth connections use the account you authorised. Some offerings instead use a
credential supplied by the server operator; those act as that shared service
account. Check with the operator if the account identity matters for your task.

## Pause or remove a connection

Use the connector controls in Trunkline's **MCP** panel to disable it temporarily,
enable it again or remove it. Disabling keeps the installation for later; removing
it means you will need to install it again to use it.

Removing or disconnecting a service in Octomate does not revoke the grant at the
provider. Use the provider's connected-app settings when you also want to revoke
that access.

## If a service is unavailable

Open **MCP** and check whether the connector is disabled or awaiting authorisation.
Reconnect if its authorisation is no longer valid, then retry a simple read-only
request. If it is ready but the agent cannot find it, confirm that the conversation
belongs to the same linked account and uses one of the supported agents above.
