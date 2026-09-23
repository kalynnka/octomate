# Built-in tentacles

Octomate includes three generic MCP tentacle types. The Slack tentacle is also
an MCP tentacle.
Configure them in `tentacles.yaml` under `tentacles:`. Each enabled offering
appears in Trunkline's **MCP → Tentacles** for people to install.

| Type | Connects to | Setup |
|---|---|---|
| `bare` | An MCP server with no authentication or one deployment token | Supply the endpoint and optional token |
| `oauth_discovery` | An MCP server that advertises its OAuth configuration | Supply the endpoint and configure Octomate's OAuth settings |
| `oauth` | An MCP server using an OAuth app you register | Supply the app credentials, scopes and supported flows |
| Slack with `mcp: true` | Slack tools acting as the linked person | Enable the channel's MCP option and user OAuth |

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
Configure [OAuth encryption](../../installation/settings.md#profile-linking-and-mcp-authorisation)
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
client id, credentials and flow endpoints. The [GitHub preset](presets.md) is one example.
For another service, use the
[OAuth template fields](../../api/config/mcp.md#octomate.config.mcp.base.OAuthMcpConfig).

## Slack tools

The Slack channel can also offer tools that act as the linked person. Set
`mcp: true` and configure its user OAuth app as described in
[Slack's MCP setup](../../usage/channels/slack.md#mcp-tools-acting-as-the-person).

## Make the connector available to an agent

After checking the settings and restarting Octomate,
[install the offering and connect your account in Trunkline](trunkline.md#install-a-configured-offering).
Use the authorisation methods that provider supports. A connector without OAuth needs
no provider consent step.

[Use and manage connections](../../usage/mcp/proxy.md) covers supported agents,
using the service across channels and managing access after setup.
