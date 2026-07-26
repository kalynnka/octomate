# Plan: self-service OAuth and MCP OAuth connections

## Status

The identity prerequisite and connection persistence are implemented. Private channel
authorization UX is the next delivery stage. This document does not add CLI or administrator
connection workflows.

## Decision

Octomate has no public account-registration system. The `users:` section of `octomate.yaml` is the
current authority that creates a durable human record and links that human's channel profiles.
Removing the YAML declaration retains the user record but unlinks its profiles. Everyone else who
can reach Octomate through an admitted channel remains a visitor and may converse without personal
integrations.

OAuth connections are self-service secrets. A connection may be started, replaced, inspected, or
revoked only from a channel profile already linked to its owning YAML user. No route, tool, or
manager API accepts a target user supplied by an administrator or by the model. The active sender
is the target.

The host operator remains part of the technical trust boundary of a self-hosted deployment: an
operator with the database, process, and encryption key can extract secrets. The product workflow
must nevertheless never ask an administrator to receive, paste, or authorize another user's token.

## Concepts

### Registered user and visitor

- `User` is currently created only by YAML reconciliation and has a stable `username` equal to its
  YAML key. Its row remains when the declaration is removed.
- `UserProfile.user_id` is nullable. A linked profile belongs to its YAML user; an unlinked profile
  is an observed visitor identity.
- An admitted visitor receives normal conversational responses but has no personal OAuth or MCP
  toolsets.
- A registered person speaking from an undeclared channel profile is a visitor until that profile
  is added to the same YAML user.
- Connections owned by a retained user with no linked profile are dormant: they remain encrypted
  in storage but no sender can inspect, use, replace, or disconnect them.

### Provider OAuth connection

A provider connection authorizes Octomate to call a provider API directly on behalf of the current
user. GitHub App user authorization and Linear user OAuth are examples. Provider OAuth uses an
Octomate-owned, pre-registered OAuth application and provider-specific endpoints.

### MCP OAuth connection

An MCP connection authorizes one user to one remote MCP resource. It follows the MCP OAuth client
flow: protected-resource discovery, authorization-server discovery, PKCE, optional dynamic client
registration, token refresh, and resource-bound bearer authentication. The connection belongs to
the user, not to an agent tentacle or a process-wide MCP configuration.

Provider OAuth and MCP OAuth share lifecycle policy and encrypted persistence, but they are distinct
typed variants because MCP additionally persists resource metadata and possibly dynamically issued
client information.

## Persistence

Add polymorphic `OAuthConnection` transmuters backed by one table:

```text
OAuthConnection
  id
  user_id                         FK users.id, ON DELETE CASCADE
  kind                            provider | mcp
  key                             configured provider or MCP server key
  status                          active | invalid
  subject                         provider account/workspace identity, when available
  account_label                   safe display label
  scopes
  encrypted_tokens                complete current token response
  expires_at
  created_at
  updated_at
  version                         optimistic refresh coordination

ProviderOAuthConnection
  provider                        github | linear | ...

McpOAuthConnection
  resource_url
  authorization_server
  encrypted_client_information    DCR result when the server issues one
```

Initially enforce one connection per `(user_id, kind, key)`. Supporting multiple accounts for the
same provider is a separate product decision and should not complicate the first implementation.

Add an `OAuthTransaction` table for unfinished browser flows:

```text
OAuthTransaction
  id
  user_id
  profile_id                      initiating linked channel profile
  kind
  key
  replace_existing
  ticket_hash                     hash of the private channel URL's bearer ticket
  state_hash
  encrypted_data                  state, PKCE verifier, discovery/client callback data
  expires_at
  started_at                      ticket has been redeemed
  callback_started_at             state has been claimed
  consumed_at
  version                         optimistic single-use coordination
```

Transactions are durable so a process restart does not silently change the security model. Expired
and consumed rows may be pruned opportunistically.

Tokens, refresh tokens, PKCE verifiers, and dynamically issued client secrets are encrypted as one
authenticated payload. The encryption key comes from deployment secrets, never YAML or the
database. Support a primary key plus old decryption keys so key rotation can re-encrypt existing
connections.

Configured connection definitions are safe YAML metadata:

```yaml
oauth:
  connections:
    github:
      kind: provider
      provider: github
    linear-mcp:
      kind: mcp
      resource_url: https://mcp.linear.app/mcp
```

Encryption keys are deliberately separate environment-only settings. The primary id chooses the
write key; every key in the JSON mapping remains available for decryption during rotation:

```text
OCTOMATE_OAUTH_PRIMARY_KEY_ID=2026-07
OCTOMATE_OAUTH_ENCRYPTION_KEYS={"2026-07":"<base64url-32-byte-key>","old":"..."}
```

`ConnectionManager` is created only when at least one connection is configured, so deployments
that do not offer personal OAuth require no encryption settings.

## Self-service flow

1. A message arrives and `ThreadManager` replaces its boundary sender with the persisted
   `UserProfile`.
2. A connection capability derives the user only from that current sender. If `user_id` is null,
   it returns the visitor explanation and creates nothing.
3. The capability creates a short-lived, single-use transaction for the requested configured
   connection. Its API has no `user_id` argument.
4. Octomate sends the authorization URL privately to the initiating profile. In a group surface it
   must DM the user; if the channel cannot deliver privately, ask the user to open a private chat
   with Octomate. Never expose the bearer ticket in a group reply.
5. The browser presents the ticket to a narrow start endpoint. The endpoint verifies its hash,
   expiry, unused state, linked profile, and configured connection before redirecting to the
   provider or MCP authorization server with `state` and PKCE.
6. The callback verifies `state`, exchanges the code, obtains a safe account/workspace label, and
   atomically stores the encrypted connection for the transaction's user.
7. The transaction is consumed and Octomate privately confirms the connected account and granted
   scopes. Tokens never appear in the response, model context, URL, or telemetry.

The same owner-only rule applies to status, disconnect, and replacement. Reauthorization must not
silently overwrite an active connection: show the existing safe label and require an explicit
replacement action from a currently linked profile of that user.

Removing a YAML user declaration is not connection revocation. Reconciliation unlinks the user's
profiles and retains both the durable user and encrypted connections. Re-adding the same stable
username restores access through newly declared profiles. A future explicit user-deletion workflow,
if introduced, must revoke connections before deleting the user; YAML removal must not impersonate
that workflow.

## OAuth clients

### Provider OAuth

Use Authlib's async Starlette/FastAPI client for authorization URL construction, code exchange, and
standard refresh behavior. Keep the small GitHub and Linear differences in their provider-owned
configuration/code; do not introduce a generic provider framework until a third provider requires
it.

- GitHub: prefer a GitHub App user access token with expiring tokens enabled. The token's effective
  permission is the intersection of the app and user permissions.
- Linear: request user authorization when work should be attributed to the user. Persist every new
  rotating refresh token in the same transaction that replaces the access token.

### MCP OAuth

Use `mcp.client.auth.OAuthClientProvider` from the installed MCP Python SDK, with:

- an Arcanus-backed `TokenStorage` bound to exactly one `(user_id, MCP server)` connection;
- redirect and callback handlers backed by `OAuthTransaction`, rather than FastMCP's default local
  browser and localhost callback;
- a pre-registered client for servers such as GitHub that require the host application to register;
- dynamic client registration only when the discovered authorization server supports it.

Do not use FastMCP's default in-memory token storage in the server. Do not copy MCP access tokens
into the provider-connection variant: OAuth tokens are resource/audience-specific even when the
same upstream account granted them.

## Refresh and revocation

- Resolve a valid token immediately before constructing a personal toolset.
- Serialize refresh by connection id in one process and use the `version` column to reject a stale
  write across processes.
- Replace the entire encrypted token document atomically; this is required for rotating refresh
  tokens.
- On `invalid_grant` or an unrecoverable 401, mark the connection invalid and tell that user to
  reconnect. Do not fall back to a process-wide/operator credential.
- Disconnect first calls the advertised/provider revocation endpoint when supported, then deletes
  the local encrypted material even if remote revocation reports an already-invalid token.

## MCP runtime integration

The current GitHub and Linear MCP toolsets are process-wide and warmed with one configured bearer
token. Personal MCP access changes the ownership boundary:

1. Resolve the triggering message's persisted profile and optional YAML user.
2. Ask `ConnectionManager` for that user's active connections.
3. Build additional MCP toolsets for that run only and pass them through Pydantic AI's per-run
   `toolsets` argument.
4. A visitor, or a registered user without a connection, receives no personal provider toolset.
5. Open and close personal MCP sessions per run initially. Add a bounded `(user_id, connection_id)`
   session pool only after measurements justify it.

Static operator tokens must never be a fallback for personal tools. When personal OAuth ships, the
existing GitHub/Linear token configuration is either removed or retained only as an explicitly
separate system/service-account feature with its own policy and no visitor access.

## Delivery stages

### 1. Identity prerequisite

- YAML-created durable `User` rows with stable usernames.
- Nullable visitor profiles.
- No runtime link-code, user merge, or administrator linking API.
- Current sender is available to run dependencies as the authorization principal.

### 2. Connection persistence — implemented

- Polymorphic connection schemas/models and migration.
- Encrypted token codec with key rotation.
- Durable transaction store and expiry cleanup.
- Connection manager with owner-bound begin, complete, get-token, replace, and disconnect methods.

### 3. Private channel authorization UX

- Connection capability whose public arguments contain only the connection key, never a user id.
- Slack/Lark/private-channel delivery of single-use links.
- Narrow start/callback routes owned by the project-level Octomate FastAPI application.
- Safe success, denied, expired, and reconnect messages.

### 4. Provider OAuth

- GitHub App user connection.
- Linear user connection with rotating refresh tests.
- Direct API consumers, if any, resolve tokens through `ConnectionManager`.

### 5. MCP OAuth and per-run toolsets

- Arcanus-backed MCP `TokenStorage`.
- Custom MCP redirect/callback coordination.
- Linear MCP OAuth, followed by GitHub MCP OAuth with the registered client.
- Remove personal GitHub/Linear toolsets from startup warming and attach them per run.

## Acceptance

- An unknown but channel-admitted sender can converse and has no `User` or OAuth connection.
- A YAML-linked sender can privately authorize only their own connection.
- A visitor, model-generated id, callback parameter, or administrator-facing route cannot choose a
  target user.
- Group messages never contain connection bearer tickets.
- A callback cannot be replayed, used after expiry, or completed for a different user/provider.
- Tokens and client secrets are encrypted at rest and absent from logs, traces, prompts, exceptions,
  and serialized public schemas.
- Concurrent refresh preserves the newest rotating refresh token.
- Removing a YAML user declaration retains its encrypted connections but makes them inaccessible;
  removing one profile only makes that profile a visitor and does not expose or transfer the user's
  connections.
- Visitors and unconnected users receive no personal MCP tools and never inherit a global token.
- GitHub and Linear MCP calls execute with the triggering user's connection and cannot cross user
  boundaries under concurrent runs.
