# Use and manage connections

Use the services you have connected to Octomate from your agents, then manage
access as your needs change.

## Before you start { #installing-per-user }

For account ownership, operator trust and namespaces, start with the
[MCP overview](../../tentacles/mcp.md). To connect a service, follow
[Add through Trunkline](../../tentacles/mcp/trunkline.md) or the
[preset walkthrough](../../tentacles/mcp/presets.md#connect-it-to-your-account),
which also covers connecting through an agent conversation.

If an installed OAuth connection is awaiting consent,
[complete authorization](../../tentacles/mcp/trunkline.md#complete-oauth-authorization)
before using its tools.
{ #authorising }

## Use it from an agent

Start with a read-only request to check access:

> Use my work tracker connection to list the tasks assigned to me.

Name the connection when you have more than one for the same service. Then ask for
changes as needed, being clear about which account or workspace to use.

These connectors are available to Inkling, driven Claude Code and Codex, and
native agents connected through [Octomate MCP](octomate.md). Driven DeepSeek
Harness cannot use them yet.

## Use the connection across channels

[Link each channel profile](../../installation/accounts.md#link-your-channel-profiles)
to the Octomate account that owns the connection. You can then refer to the same
namespace from another channel without installing the service again.

## Pause or remove a connection

Ask your agent to pause a connection:

> Disable my tracker_work connection until I need it again.

Disabling keeps the installation and saved authorization. Ask the agent to enable
it again, or select **Disabled** in Trunkline's **MCP → Installed** table.

To remove a connection, select its remove button in **Installed**, then
**Confirm remove**. This deletes the installation and its saved credentials;
you will need to install it again to use it.

Removing or disconnecting a service in Octomate does not revoke the grant at the
provider. Use the provider's connected-app settings when you also want to revoke
that access.

## If a service is unavailable

Open **MCP → Installed** and check whether the connection is disabled or awaiting
authorization. [Reconnect](../../tentacles/mcp/trunkline.md#complete-oauth-authorization)
if its authorization is no longer valid, then retry a simple read-only request.
If it is ready but the agent cannot find it, confirm that the conversation belongs
to the same linked account and uses one of the supported agents above.
