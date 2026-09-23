# MCP proxy

Octomate stands between an agent and the vendor MCP servers its users have
connected. The operator declares a server once; each user installs it for
themselves and authorises it with their own account; the agent then reaches it as
that person. Nothing is shared across users, and the server's own credentials never
leave the server.

It is a proxy in a narrow sense. Installed tools are not re-exported as tools of
their own. An agent discovers a connector with `mcp_list_servers`, loads its tools
with `mcp_list_tools`, and calls one with `mcp_call_tool`, which forwards the call
and returns the upstream's result unchanged. Arguments are validated by the
upstream, not twice. That keeps a per-user tool list out of the prompt prefix,
where it would fork every cached prompt.

## Declaring a connector

Three template types cover any remote server, all under `tentacles:`:

```yaml
tentacles:
  linear:                          # discovered OAuth, no app registration
    type: oauth_discovery
    url: https://mcp.linear.app/mcp
  notion:                          # one operator token for every caller
    type: bare
    url: https://mcp.notion.com/mcp
    token: ntn_...                 # or OCTOMATE__TENTACLES__NOTION__TOKEN
  github:                          # a registered OAuth application
    type: oauth
    url: https://api.githubcopilot.com/mcp/
    client_id: Iv1...
    scopes: [repo, read:org, read:user]
    scope_separator: ","
    flows:
      - type: device
        device_authorization_endpoint: https://github.com/login/device/code
        token_endpoint: https://github.com/login/oauth/access_token
      - type: authorization_code
        authorization_endpoint: https://github.com/login/oauth/authorize
        token_endpoint: https://github.com/login/oauth/access_token
        token_endpoint_auth_method: client_secret_post
```

| Type | Credential | Needs |
|---|---|---|
| `bare` | The deployment's own token, or none. Every caller is the deployment. | Nothing, or `oauth.encryption_key` when a token is set |
| `oauth_discovery` | Each user's grant, obtained by discovering the server's OAuth metadata and registering a client dynamically, or through a hosted client metadata document | `oauth.encryption_key`, `oauth.callback_base_uri` |
| `oauth` | Each user's grant, through an application you registered with the provider | `oauth.encryption_key`; `callback_base_uri` too for an authorization-code flow |

`octomate mcp preset github --client-id <id>` writes the GitHub block above into
the config home for you, with GitHub's endpoints and scopes filled in. Put the
client secret in `OCTOMATE__TENTACLES__GITHUB__CLIENT_SECRET`. The `oauth` type's
`flows` lists what the provider supports; the first is the default, and a
`client_secret` must match the token endpoint's authentication method.

Slack is the fourth: its [channel tentacle](../channels/slack.md#mcp-tools-acting-as-
the-person)
is also a connector when `mcp: true`.

A URL must be HTTPS and resolve to a public address; a redirect is refused, so
declare the final endpoint. The one exception is a `bare` server, which accepts a
plain URL for a server on your own network.

## Installing, per user

Declaring a connector makes it an **offering**. Each user installs it, from
Trunkline's MCP panel, from a chat by asking the agent, or over the account API:

```
mcp_list_tentacles                     what is on offer
mcp_install name namespace url         install one; tentacle_id for an offering
mcp_list_servers                       what I have, enabled or not, authorised or not
mcp_enable / mcp_disable / mcp_uninstall
```

An installation has a **namespace** you choose, stored as `personal/<name>`, which
is what the tool calls name. A remote server can also be installed by URL without
an offering, either unauthenticated or with discovered OAuth. Personal bearer
tokens are entered through the authenticated HTTP API, never as a tool argument.

Installing enables the connector. It does not authorise it.

## Authorising

For an OAuth connector the user connects once:

- **Device flow**: `oauth_connect` sends the code and link to the user's direct
  messages; `oauth_confirm` polls until the provider approves.
- **Authorization code**: `oauth_connect` sends a start link to the user's DMs; the
  browser round-trips through `<callback_base_uri>/oauth/<tentacle>/callback`, and
  the landing page says "Connected" and nothing else. `oauth_confirm` then reports
  it active.

Links go to a private surface, never to the group, and are never returned to the
model. A pending device authorisation is resumed rather than restarted. Stored
grants are encrypted with `oauth.encryption_key`, refreshed shortly before expiry,
and retired to "invalid" when the provider rejects a refresh, at which point the
user reconnects. One registered application can authorise several installed
servers. Disconnecting revokes nothing at the provider.

`oauth_link_profile` is a different flow with the same delivery: it links the
channel profile driving the turn to an Octomate account. See
[Accounts and tokens](../../installation/accounts.md#link-your-channel-profiles).

## Connections and the pool

Upstream clients are cached per user and per installation, closed after
`mcp_pool.idle_timeout` seconds without a call (an hour), and closed immediately on
disable or uninstall. Every request refuses to follow a redirect and refuses a
private or link-local address, so an installed credential can never be sent
somewhere other than the endpoint it was installed for.

```yaml
mcp_pool:
  idle_timeout: 3600
```

## Where the tools show up

- **Inkling** mounts the connector tools in process, deferred so the person's list
  never touches the prompt prefix, and the server's instructions tell it to check
  them before saying a tool does not exist.
- **Driven Claude Code and Codex** reach them through the Octomate MCP server they
  are given.
- **Native sessions** reach them through their installed `octomate` MCP entry.
- **Driven dsh** has no path to them yet.
