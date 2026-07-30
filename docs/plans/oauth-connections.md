# Plan: self-service OAuth connections

## Status

Stages 1–4 are **finished**, and stage 5 has both its connectors. The YAML user registry and
provider-neutral connector foundation are complete, and there are now two concrete connectors,
one per flow:

- **GitHub** — device flow. A registered user on any channel authorizes with a code and then uses
  the GitHub MCP server with their own encrypted token. A pending authorization is reused until it
  expires rather than reissued.
- **Linear** — authorization code with PKCE over the direct-HTTP transport. Linear has no device
  grant, which is what finally made stage 4 necessary. A registered user opens a UUID-only link,
  approves in a browser, and the callback finishes the connection by itself.

~~registered Slack users can authorize with device flow, confirm from Slack~~ — confirmation is no
longer a channel affordance; see [Confirmation](#confirmation--superseded).

Token refresh is implemented, because Linear's access tokens last 24 hours and an integration that
died daily would not be one. Revoking at the provider and a deliberate disconnect are still not.
A revoked credential retires itself ([Revocation](#revocation--finished)).

The relay transport is still a named placeholder, now deliberately rather than incidentally — see
[stage 4](#4-authorization-code-transports--finished).

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
- A concrete agent capability owns its connector composition and user-facing tools. Application
  bootstrap builds and registers the GitHub device connector once, then injects that connector
  into each GitHub capability. A user-bound instance exposes either OAuth tools or an authenticated
  MCP toolset for the initiating user's agent run without mutating the connector registry.
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
are injected strategies. This let GitHub choose device flow first and Linear choose authorization
code plus direct HTTP afterwards, without either adding a branch inside the manager.

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
- A completed connection is not activated until the initiating channel identity confirms it. No
  operation secret or user selector appears in the tool arguments.
  **Narrowed to device flow** — an authorization-code callback is itself the notification that the
  user acted, and a second confirmation could check nothing about it; see
  [stage 4](#4-authorization-code-transports--finished) for what is kept in its place.
  ~~Slack DM delivery is not available yet, so the device link/code and confirmation are sent back
  to the originating Slack conversation, including a group or thread when that is where the request
  came from.~~ **Superseded** — `ChannelTentacle.open_dm` shipped with
  [dm-and-cross-channel-continuation.md](dm-and-cross-channel-continuation.md) §1, so the
  compromise this recorded is over. `OAuthFeeler.deliver_to` now routes an authorization asked for
  in a group into that person's direct messages; a channel with no DM surface keeps the
  conversation it came from, and a channel that has one but fails to open it raises rather than
  reading a one-time code out to a group.
- Token refresh replaces the entire encrypted token response atomically. Rotating refresh tokens
  must not be lost to concurrent refreshes.
- MCP credential caches compare the connection subject and normalized granted scopes, not only the
  access token. A refreshed token may be swapped into a retained mutable `httpx.Auth` object only
  when both subject and scopes are unchanged. A changed or unknown subject/scope set requires the
  old MCP session to close and a new toolset to initialize and fetch `tools/list` before use.
- Pydantic AI caches `MCPToolset.list_tools()` until the server sends
  `notifications/tools/list_changed` or the session closes. Changing client authentication does not
  guarantee that notification, while MCP permits tool listings to depend on request authorization.
  Octomate must therefore invalidate by rebuilding on scope changes rather than reuse a stale tool
  cache. GitHub remote OAuth may challenge for a missing scope instead of hiding the tool, but that
  provider behavior is not a safe cross-provider assumption.
- Visitors and unconnected users never inherit a process-wide/operator credential.
- Removing a YAML declaration makes retained connections dormant; it does not transfer or silently
  revoke them.

## Delivery stages

### Confirmation — superseded

The original design finished a connection from the message that started it:

> ~~A card carries the whole errand — the link, the code, and a confirm button that finishes the
> connection from the card itself, so the user never has to come back and say so. The card rewrites
> in place with the outcome, and the confirm button carries the authorization back with it so a
> press that lands early can redraw the same card with a note.~~

**Dropped.** A press only reaches Octomate over Feishu's `card.action.trigger` 回调, and neither
transport is available here — 长连接 is enterprise-only on this tenant, and there is no public
ingress for a request URL. Slack's equivalent worked but is gone too, so one flow serves every
channel.

The user now tells the agent they have authorized, and the capability's confirm tool calls
`complete_latest`. That keeps every property this section cared about: the tool takes no arguments,
so no operation secret or user selector crosses the model, and completion is bound to the profile
driving the run. What is lost is the card rewriting itself with the outcome — the agent's reply is
the acknowledgement instead.

### Revocation — finished

~~`OAuthConnectionStatus` is `Literal["active", "invalid"]` and nothing ever sets `"invalid"`, so a
token the user revokes at GitHub stays "active" forever: `access_token` keeps handing it out, every
MCP call 401s, and there is no path back.~~

Only the provider can say a credential is gone, so the thing that talks to it brings the news back.
`ConnectionAuth` is an `httpx.Auth` carrying one user's bearer token into their MCP session; a 401
answering it is reported once to `OAuthManager.invalidate`, which marks the connection `"invalid"`.

It sits on the transport rather than in a tool hook because that is the only place that sees the
whole session. The same 401 answers the `initialize` that warms a session and the tool call that
uses it, and `McpToolsetCache.warm` logs a failure and moves on — so a revoked token would
otherwise fail quietly on every run forever, never reaching a hook at all. It also sidesteps
`ToolFailureCapability`, which implements `on_tool_execute_error` too and would make the outcome
depend on capability order.

Recovery needed nothing new: `access_token` already filters on `status == "active"`, so an
invalidated connection reads as none, and `for_profile` mounts `connect_github` with its
instruction instead of the MCP toolset. `access_token` also records the expiry it was already
silently rejecting, which is the same failure a connector with expiring tokens will hit.

`OAuthManager.connection_status` is what separates never having connected from having connected and
lost it — `access_token` collapses both to no token, which is right for using one and wrong for
explaining its absence. A retired connection gets its own instruction telling the model to raise it
unprompted, since the user cannot see that tools they were using are gone.

The turn that discovers the 401 still has MCP tools mounted and fails as a `ToolFailed` the model
explains; the turn after it says the connection went stale and offers to reconnect. Closing that lag means re-reading the connection
after `acquire`, a query per run to save one degraded turn — not taken. The dead session also stays
in the toolset cache until LRU eviction, unreachable because no token resolves to it.

Still open: nothing revokes the token *at* GitHub, and there is no `disconnect_github` for a user
who wants out deliberately rather than because the provider ended it.

### 1. YAML user identity — finished

- Durable users keyed by YAML username.
- Cross-channel profiles linked to the same user.
- Ownerless visitor profiles for admitted unknown senders.

### 2. Connector foundation — finished

- `OAuthConnector` composed from a flow and optional callback transport.
- Device and authorization-code flow contracts.
- Direct HTTP and relay callback-transport contracts.
- `OAuthManager` connector registry and `UserManager` principal resolution.
- No provider, MCP, route, relay, token, or connection implementation.

### 3. GitHub device OAuth — finished

- GitHub capability building its device connector for one-time application registration and
  receiving the registered connector in every run-scoped instance.
- Durable encrypted operation and connection storage, with the schema generated from this concrete
  lifecycle rather than provider/MCP inheritance.
- Owner-bound device-code presentation on every channel, ~~and explicit confirmation from Slack~~
  with confirmation through the agent (see [Confirmation](#confirmation--superseded)).
- ~~The bare verification message is emitted to the originating Slack conversation; DM routing is a
  later channel enhancement.~~ An authorization asked for in a group is delivered to the asker's
  direct messages.
- A pending authorization is resumed until it expires; asking again does not mint a second code.
- Connection replacement is implemented, and a revoked credential now retires itself
  ([above](#revocation--finished)); owner-bound refresh and disconnect remain.

### 4. Authorization-code transports — finished

~~`DirectHttpOAuthCallbackTransport` and `RelayOAuthCallbackTransport` exist and override only
`kind` … neither class can be instantiated.~~ `DirectHttpOAuthCallbackTransport` is real. It takes
one thing, the base URI a browser reaches this deployment at, and turns it into the two paths in
`schemas/oauth.py` that `oauth/routes.py` serves — shared constants, so the link a user opens and
the route waiting for them cannot drift apart.

**What only the browser has to reach.** An authorization server never connects to the callback; it
redirects the user agent. The token exchange is outbound. So the requirement is a stable HTTPS-or-
loopback address *the authorizing browser* can resolve, not public ingress — which is why
`http://127.0.0.1:8000` is a working default and a tunnel or domain is a config change, not a
design change.

- **Durable state and PKCE operation data.** The manager mints the state, the flow returns the PKCE
  verifier, and both are sealed into the operation's encrypted payload along with the redirect URI
  the token exchange must replay and the provider URI the start route redirects to.
- **State names its own operation.** A provider redirects to one registered URI per connector, with
  no room to say which authorization came back, so the state is `{operation_id}.{random}` and the
  row is found by its prefix and then matched on the whole value with `compare_digest`. The
  operation id alone will not do: it travels in the public start link.
- **Narrow project-level start/callback routes for direct HTTP.** Two `GET`s, mounted by
  `Octomate.app` only when a registered connector actually points a browser at them. They answer
  with a page rather than JSON, and every refusal — unknown, spent, expired, mismatched, another
  connector's — is the same page, so guessing teaches nothing.
- **Relay implementation — deliberately not built.** A relay earns its keep only where no stable
  URL is possible at all, and it costs a deployed function, a store, a shared secret and a polling
  loop. A tunnel makes `direct_http` work instead, and the two produce identical Octomate code.
  `RelayOAuthCallbackTransport` stays abstract until a deployment genuinely cannot have a URL.
- **Replay, expiry, denial, unlinking, and confirmation tests** — all present, at the manager and
  through the real router.

**Confirmation, deliberately different here.** The security requirement below says a completed
connection is not activated until the initiating channel identity confirms it. That was written
when the only flow was device polling, where Octomate cannot otherwise know the user finished. An
authorization-code callback *is* that notification, and a second channel confirmation could verify
nothing about it — the confirm tool takes no arguments by design. So the callback activates the
connection, and everything the requirement was protecting is kept at that moment instead: the
operation is single-use, short-lived, bound to the initiating user and profile, and completion
rechecks that the profile still resolves to the same registered user before the code is spent.
`confirm_linear` reports status rather than advancing anything.

### 5. MCP OAuth — GitHub and Linear paths finished

- MCP connector using authorization-server discovery and the appropriate injected flow/transport.
- Per-user token storage and per-run capabilities are implemented for both providers. A bound
  capability exposes OAuth tools before connection and the authenticated MCP toolset afterward. The
  configured capability caches and keeps one MCP session warm per registered user across agent runs.
  The configured GitHub API token has been removed, and Linear's operator MCP token with it; an
  ownerless visitor receives neither OAuth nor MCP tools.
- **The shared shape is now shared code.** Two integrations differing only in flow made the
  duplication real, so `OAuthMcpCapability` owns the per-user machinery — binding, the toolset
  cache, retirement, the event injection — and `GitHubCapability`/`LinearCapability` supply their
  flow type, tool names and prose. Neither Linear nor GitHub uses dynamic client registration:
  both MCP servers accept the provider's own OAuth bearer token, which is what let stage 5 skip
  discovery entirely for now.
- Refresh is implemented, and returns a whole grant — access token, refresh token, subject, granted
  scopes, expiry — which `store_grant` writes over the connection atomically. Refreshes are
  serialized and re-read under the lock, so a rotating refresh token is never spent twice by two
  runs racing on the same near-expired connection.
  **Precondition, still unmet:** the cache is keyed on the access token alone
  (`fingerprint=access_token.get_secret_value()`), not on subject and normalized scopes as the
  security requirement above demands. This is now a live cost rather than a theoretical one —
  Linear tokens last 24 hours, so every daily refresh drops a warm session it was supposed to keep.
  It is a wasted warm-up, not a correctness bug: a changed fingerprint rebuilds, which is the safe
  direction.
- General MCP authorization-server discovery remains for later connectors.
- Dynamic client registration only when advertised and required.

## Acceptance

One of these is not met yet and is marked so — the rest hold.

- Connector construction rejects invalid flow/transport combinations.
- An unknown connector is rejected before any OAuth work starts.
- A visitor cannot start OAuth; a YAML-linked sender can start only for themselves.
- No public manager method accepts a target user id or username. The callback boundary takes a
  connector id, a state and a code, and derives the user from the operation the state names.
- Authorization-code provider state is staged behind a UUID-only user-facing URI.
- GitHub uses the generic manager without adding GitHub branches to `OAuthManager`, and so does
  Linear — the manager knows flows and transports, not providers.
- A replayed, expired, declined, or mismatched callback cannot produce a connection, and a profile
  unlinked mid-flow cannot finish one.
- A token-only refresh with unchanged subject and scopes preserves the warm MCP session; a subject
  or scope change produces a new authorization-aware tool listing. **Not met** — the cache is keyed
  on the token alone, so any refresh drops the session
  ([stage 5](#5-mcp-oauth--github-and-linear-paths-finished)).
- An authorization asked for in a group reaches only the person who asked, with or without a code
  to read out.
- A later MCP connector can reuse the same manager without being modeled as a provider subclass.
