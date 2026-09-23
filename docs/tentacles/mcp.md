# MCP connectors

An MCP tentacle makes a tool service available through Octomate. The operator
configures the connection once; each person installs it for their account.
Choose the type that matches the service's authentication.

## No authentication or a deployment token

Use `bare` for a server that needs no OAuth flow:

```yaml
tentacles:
  tools:
    type: bare
    enabled: true
    url: https://tools.example.com/mcp
```

Replace the URL with your endpoint. If it needs a bearer token, add `token` in
the YAML block or supply `OCTOMATE__TENTACLES__TOOLS__TOKEN` in the environment.
Configure [OAuth encryption](../installation/settings.md#profile-linking-and-mcp-authorisation)
when storing credentials. A deployment token gives every caller the same upstream
identity; choose a per-user OAuth connector when individual grants are needed.

## Discovered OAuth

Use `oauth_discovery` when the service advertises its OAuth metadata and supports
dynamic client registration or a hosted client metadata document:

```yaml
tentacles:
  tools:
    type: oauth_discovery
    enabled: true
    url: https://tools.example.com/mcp
```

Supply the service's actual HTTPS endpoint and configure `oauth.encryption_key`
and `oauth.callback_base_uri`. Each user completes the service's consent flow.

## A registered OAuth application

Use `oauth` when you register an application with the provider and supply its
client id, credentials and flow endpoints. For GitHub, the CLI fills in the
endpoints and scopes:

```sh
octomate mcp preset github --client-id '<your-oauth-app-client-id>'
```

Run it with `OCTOMATE_HOME` pointing to the server's config home. It adds a
`github` block and refuses to replace an existing id. Provide the app's client
secret in YAML or `OCTOMATE__TENTACLES__GITHUB__CLIENT_SECRET`, configure the
[OAuth origin and encryption key](../installation/settings.md#profile-linking-and-mcp-authorisation),
and register the callback URL with the provider. For another service, use the
[OAuth template fields](../api/config.md#octomate.config.mcp.base.OAuthMcpConfig).

## Slack tools

The Slack channel can also offer tools that act as the linked person. Set
`mcp: true` and configure its user OAuth app as described in
[Slack's MCP setup](../usage/channels/slack.md#mcp-tools-acting-as-the-person).

## Make the connector available to an agent

After checking the settings and restarting Octomate, open **MCP** in Trunkline,
install the offering and complete consent if required. Installing enables the
connector; it does not complete OAuth authorisation. Ask your agent to list the
connector's tools and make a read-only call to verify access.

[Using the MCP proxy](../usage/mcp/proxy.md) explains per-user installations,
consent, disabling connectors and which agent runtimes can use them.
