# Adding an MCP tentacle

Most connectors need no code. A `bare`, `oauth` or `oauth_discovery` block in
`tentacles.yaml` declares a remote MCP server, and `build_mcp` turns it into an
`McpTentacle` users can install. See [MCP proxy](../usage/mcp/proxy.md).

A bespoke tentacle exists for one reason: the connector must behave differently
from a generic declaration. Slack's does, because its tools must act as the same
person the channel is talking to, and because it ships instructions of its own.

## The class

`McpTentacle` is a description: an `auth_kind`, an optional operator `token`, a
`label`, the `upstream` URL, `instructions` shown to the model with the tool
catalog, and `serving`, which says whether the connector is on at all. Two
variants ship: `BareMcpTentacle` for an operator credential and `OAuthMcpTentacle`
for per-user grants.

To write one:

```python
class MyServiceTentacle(OAuthMcpTentacle):
    """My Service's MCP server, acting as the person who connected it."""

    label = "My Service"
    upstream = "https://mcp.myservice.example/mcp"
    instructions = "The tools act as the connected person. Ids are ..."

    def __init__(self, id: str, octomate: Octomate, *, config: MyServiceConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        octomate.oauth.register(OAuthConnector(
            id=id,
            mcp_url=self.upstream,
            flows=[AuthorizationCodeFlow(...)],
            callback_transport=DirectHttpOAuthCallbackTransport(config.callback_base_uri),
        ))

    @property
    def serving(self) -> bool:
        return self.config.mcp
```

The OAuth connector is what the OAuth manager uses to start and complete a user's
authorisation and to refresh their tokens; `resolve_profile` on it is optional, and
is how Slack and Discord link a channel profile from a successful grant.

Register the config model in `McpConfigVariant` and add a case to `build_mcp` in
`octomate/tentacles/mcp.py`. `Octomate.connect` files every `McpTentacle` with the
MCP manager, which is what makes it an offering.

A channel can be a connector too, as Slack is, by inheriting both bases and
letting a channel flag decide `serving`.
