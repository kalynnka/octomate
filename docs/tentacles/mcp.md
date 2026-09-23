# MCP

Connect MCP tool services to Octomate and use them from your agents. You can
install an offering configured by the operator or add your own endpoint in
Trunkline.

## Connections belong to you

MCP installations are specific to your Octomate account. An operator enabling a
tentacle makes it available to install; each person must connect it before their
agents can use it. For OAuth services, install the connection and complete the
provider's sign-in and consent through [Trunkline](mcp/trunkline.md), or ask your
agent to help in a [linked conversation](../installation/accounts.md#link-your-channel-profiles).
The conversation must support private delivery of the authorization link or
device code. You complete sign-in on the provider's page; the agent does not
receive your password. See the
[authorization steps](mcp/trunkline.md#complete-oauth-authorization).

!!! warning "Only connect through an operator you fully trust"
    Authenticate or supply a token only if you completely trust the operator of
    the Octomate service, for example when you run it yourself. Any required
    bearer tokens or OAuth credentials are persisted in the server's database.
    They are encrypted at rest, but the operator controls the server and its
    encryption key and can access those credentials.

Connections that use a bearer token or require no authentication do not need
OAuth sign-in. A token supplied by the operator uses a shared service identity,
even though each person's installation is separate.

## Namespaces and proxying

Each installation has a namespace, such as `personal/github_work`. You choose
`github_work` when installing; Octomate adds `personal/`. The name is unique
within your account and cannot be changed after installation. Separate
namespaces let you connect multiple accounts or workspaces from the same service.

Octomate proxies tool calls for the user in the conversation. Your agent discovers
your enabled connections, looks up a connection's tools by namespace, and asks
Octomate to call them. Octomate contacts the upstream MCP server with that
installation's credentials and returns the result. You can reuse the connection
across [supported agents](../usage/mcp/proxy.md#use-it-from-an-agent) without
configuring that service in each harness. Link your chat profiles to the same
Octomate account to use those connections across channels.

## Connect a service

<div class="grid cards" markdown>

- **[Built-in tentacles](mcp/builtins.md)**
    { #built-in-tentacles }

    Configure `bare`, `oauth_discovery` or `oauth` connections, or Slack's MCP
    integration, in `tentacles.yaml` to offer services to everyone on the deployment.

- **[Add presets](mcp/presets.md)**
    { #built-in-presets }

    Set up the packaged GitHub configuration, including app registration,
    credentials and enabling the offering. A preset fills in provider settings;
    each person still connects their own account.

- **[Add through Trunkline](mcp/trunkline.md)**
    { #connect-it-to-your-account }

    Install a configured offering or add a custom HTTPS endpoint with no
    authentication, a bearer token or OAuth. Once the required server settings
    are in place, custom installations need no config edit or restart.

- **[Use and manage connections](../usage/mcp/proxy.md)**

    Use connected services from your agents, link chat profiles to the same
    account, and check access with a simple request. Reconnect when needed,
    disable a connection temporarily or remove it from your account.

</div>
