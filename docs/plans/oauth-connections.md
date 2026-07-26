# Plan: self-service OAuth connections

## Status

The YAML user registry and provider-neutral connector foundation are complete. GitHub is the first
concrete connector: registered Slack users can authorize with device flow, confirm from Slack, and
use the GitHub MCP server with their own encrypted OAuth token. Authorization-code transports,
other providers/channels, refresh, revocation, and disconnect are not implemented yet.

## Decisions

Octomate does not expose public user registration. The `users:` section of `octomate.yaml` is the
authority that links channel profiles to durable humans. An admitted but unlinked channel sender is
a visitor: they can converse normally but cannot create or use a personal OAuth connection.

OAuth is self-service. The initiating `UserProfile` is the only accepted principal; `OAuthManager`
resolves its owner through `UserManager`. OAuth APIs must never accept a target username or user id,
so an administrator, model-generated argument, or callback parameter cannot select another human.
Operators remain in the technical trust boundary of a self-hosted process, but product workflows
must never ask an operator to receive or authorize another user's credentials.

Provider APIs and MCP servers use the same connector boundary. They are not persistence variants.
Each connector composes two independent choices:

```text
OAuthConnector
  ├── flow
  │    ├── device
  │    └── authorization_code
  │
  └── callback transport (authorization_code only)
       ├── direct_http
       └── relay
```

- A device flow has no callback transport.
- An authorization-code flow must select exactly one callback transport.
- The connector owns upstream-specific behavior such as endpoints, scopes, token exchange, refresh,
  revocation, account discovery, and MCP authorization-server discovery.
- The callback transport owns only how the browser start/callback crosses the deployment boundary.
  It does not own provider tokens or choose the user.
- The manager owns connector registration and the user authorization boundary.

Authorization-code secrets must not be included in a channel message. A transport stages the real
provider authorization URI and returns a public start URI containing only a random operation UUID.
Direct HTTP retrieves the staged operation from Octomate; a relay transports the same operation
without changing connector behavior. Both paths eventually return the callback to the same manager
completion boundary.

## Connector foundation

`OAuthConnector` is a composition object, not a provider base class. Its flow and callback transport
are injected strategies. This lets GitHub choose device flow first, while a future Linear or MCP
connector can choose authorization code plus direct HTTP or relay without branching inside the
manager.

`OAuthManager` belongs to the project-level `Octomate` instance and shares its `UserManager`. Starting
an authorization:

1. resolves the connector by its registered id;
2. resolves the current channel profile's YAML-linked owner;
3. rejects an ownerless visitor before invoking the connector;
4. creates an opaque operation id;
5. invokes the selected flow; and
6. for authorization code, asks the selected transport for the safe user-facing start URI.

This stage deliberately stops before durable operations and tokens. Their exact schema should be
driven by the first real connector rather than preserving the obsolete `provider | mcp` hierarchy.

## Security requirements for implementation stages

- A connection is unique per `(user, connector)` until multiple accounts are explicitly designed.
- Device codes, authorization codes, PKCE verifiers, access tokens, refresh tokens, and dynamically
  issued client secrets are encrypted at rest and absent from logs, traces, prompts, URLs sent to
  channels, exceptions, and public schemas.
- Authorization operations are durable, short-lived, single-use, and bound to the initiating user
  and profile. Completion rechecks that the profile is still linked to the same user.
- A completed connection is not activated until the initiating channel identity confirms it. Slack
  DM delivery is not available yet, so the device link/code and confirmation are sent back to the
  originating Slack conversation, including a group or thread when that is where the request came
  from. No operation secret or user selector appears in the tool arguments.
- Token refresh replaces the entire encrypted token response atomically. Rotating refresh tokens
  must not be lost to concurrent refreshes.
- Visitors and unconnected users never inherit a process-wide/operator credential.
- Removing a YAML declaration makes retained connections dormant; it does not transfer or silently
  revoke them.

## Delivery stages

### 1. YAML user identity — complete

- Durable users keyed by YAML username.
- Cross-channel profiles linked to the same user.
- Ownerless visitor profiles for admitted unknown senders.

### 2. Connector foundation — implemented

- `OAuthConnector` composed from a flow and optional callback transport.
- Device and authorization-code flow contracts.
- Direct HTTP and relay callback-transport contracts.
- `OAuthManager` connector registry and `UserManager` principal resolution.
- No provider, MCP, route, relay, token, or connection implementation.

### 3. GitHub device OAuth — first usable slice implemented

- GitHub connector using device flow.
- Durable encrypted operation and connection storage, with the schema generated from this concrete
  lifecycle rather than provider/MCP inheritance.
- Owner-bound device-code presentation and explicit confirmation from Slack.
- The bare verification message is emitted to the originating Slack conversation; DM routing is a
  later channel enhancement.
- Connection replacement is implemented; owner-bound refresh and disconnect remain.

### 4. Authorization-code transports

- Durable state and PKCE operation data.
- Narrow project-level start/callback routes for direct HTTP.
- Relay implementation using the same manager completion boundary.
- Replay, expiry, denial, unlinking, and confirmation tests.

### 5. MCP OAuth — GitHub path implemented

- MCP connector using authorization-server discovery and the appropriate injected flow/transport.
- Per-user token storage and per-run GitHub MCP toolsets are implemented. The configured GitHub API
  token has been removed; an ownerless visitor receives neither OAuth nor MCP tools.
- General MCP authorization-server discovery remains for later connectors.
- Dynamic client registration only when advertised and required.

## Acceptance

- Connector construction rejects invalid flow/transport combinations.
- An unknown connector is rejected before any OAuth work starts.
- A visitor cannot start OAuth; a YAML-linked sender can start only for themselves.
- No public manager method accepts a target user id or username.
- Authorization-code provider state is staged behind a UUID-only user-facing URI.
- GitHub uses the generic manager without adding GitHub branches to `OAuthManager`.
- A later MCP connector can reuse the same manager without being modeled as a provider subclass.
