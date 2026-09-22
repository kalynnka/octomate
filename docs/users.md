# Local accounts

Trunkline requires a local account. The operator issues invitations from the server
CLI; both web and CLI registration require an invitation.

Apply the migration before starting this version:

```sh
uv run alembic upgrade head
```

Add `auth.yaml` to the deployment's config home (`.octomate/config/`,
`~/.octomate/config/`, or `$OCTOMATE_HOME`). Generate three independent secrets with
`uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set:

```yaml
auth:
  access_token_salt: "<first generated secret>"
  refresh_token_salt: "<second generated secret>"
  api_key_salt: "<third generated secret>"
```

Access tokens last 15 minutes, sessions last 15 days, and invitations last 7 days.
The corresponding settings are `access_token_lifetime`, `session_lifetime`, and
`invitation_lifetime`, accepting ISO 8601 durations such as `PT15M` and `P7D`.
Session refresh rotates both credentials without extending the session deadline.
Keep the salts stable across restarts and shared by every process serving this
instance. Without auth configuration, Trunkline's API refuses access with 503.

Profile linking shares OAuth's public Octomate origin and browser authorization
lifetime. Add these settings in `oauth.yaml` in the same config home:

```yaml
oauth:
  callback_base_uri: "https://octomate.example.com"
  authorization_lifetime: PT10M
```

`callback_base_uri` is the browser-reachable origin serving Trunkline, without a
path, credentials, query, or fragment. It is optional for local accounts, but
channel profile linking requires both it and auth configuration. Browser OAuth
authorizations and profile links last 10 minutes by default; `authorization_lifetime`
accepts an ISO 8601 duration or a number of seconds. Provider-issued device codes
retain the provider's expiry. Profile linking alone does not require an OAuth
encryption key.

Browser cookies are HttpOnly, SameSite=Strict, and Secure. Serve through HTTPS. For
local HTTP development only, add `cookie_secure: false` under `auth:`. The console
uses the same origin as the API and supplies `X-Octomate-Request: 1` on writes;
requests without that header are refused.

Create an invitation code using the same config home and database as the server:

```sh
uv run octomate service invite
uv run octomate service user create --username alice --password 'YourPassword1!' --invitationcode '<code>'
```

Both commands require the server package and use its configured database.
`user create` consumes the code through the same registration manager as the web
API. Passwords must contain 11-1024 characters, including at least one lowercase
letter, uppercase letter, digit, and symbol. Whitespace does not count as a symbol.
`--name` sets a display name;
omitting it uses the username. The command creates the account without issuing a
session or API token. Sign in through the Trunkline UI afterwards.

To issue a registration link for the UI instead of a raw code:

```sh
uv run octomate service invite --url https://octomate.example.com
```

Each invocation creates an independent, anonymous invitation. Anyone with the
printed link can choose an unused username, a display name, and a password meeting
these requirements. Issuing the invitation does not create or reserve an account.
The token stays in the URL fragment until the registration form reads it, so opening
the link does not put it in HTTP access logs. Only its hash is stored in the database.
An invitation admits one account and cannot be reused after successful registration.
A rejected username leaves the invitation usable.

Existing usernames, including passwordless accounts, cannot be claimed with an
invitation. Accounts and profile ownership live in the database. Startup does not
seed users or change profile links. User records hold no plaintext bearer.
Hooks and MCP authenticate separate, named API keys stored only as salted hashes.

Trunkline shows threads in which one of the signed-in user's bound profiles has
spoken, including linked IM and native client history. The same access check covers
messages, conversations, projects, pending actions, permission changes, and action
responses. Console chat identities use the user's stable ID. Old console history
under the former `dev` profile remains stored and hidden until it is bound to an
owner. Channel profile linking is described below.

Account endpoints are `/api/auth/register`, `/login`, `/refresh`, `/logout`, and
`/me` under the same `/api/auth` prefix. They use browser cookies; passwords and
session tokens are excluded from response bodies. Channel-profile linking and MCP
OAuth are separate authorization flows.

## Link a channel profile

Ask the agent to link the channel profile driving the current conversation. The agent
calls `oauth_link_profile`, an Octomate MCP tool with no identity arguments. Octomate takes
the stable platform user ID from the authenticated inbound event and persists the
profile as an ownerless visitor if necessary. The tool returns only delivery status
to the model; the single-use Trunkline URL is sent directly to the user by the channel
presentation layer. From a shared conversation, that layer opens the same user's
direct messages and sends the link there. If the channel has no private surface, the
tool refuses instead of disclosing the link in the shared room. A failed private
delivery is reported as a tool error.

Opening the URL restores the browser's existing Octomate session, including through
normal refresh-token rotation. If no session remains, sign in normally; the link
ticket stays in browser memory while the login form is shown. Trunkline then displays
the exact channel, profile name, platform ID, and signed-in account for explicit
confirmation. Confirmation consumes the ticket and assigns that profile to the
signed-in user in one transaction. If another browser tab changes the signed-in
account, confirmation is refused; reopen the link to review the current account.
Opening another profile link in the same tab clears the previous confirmation.
An owned profile cannot be transferred through this flow.

After a successful Slack or Discord OAuth connection, Octomate automatically links
the verified channel profile to the authenticated account that started OAuth.
The callback uses the account bound to the OAuth operation, so it needs no second
Octomate login or confirmation, even if browser cookies are missing or have changed.
The Profile page's **Link a channel** section can start a configured channel's
OAuth flow directly, without an MCP installation. Discord requests only `identify`
and verifies the account through `/users/@me`.
For Slack, it verifies the granted user and workspace and requires the workspace to
match the configured Slack channel. Profiles belonging to another account are never
transferred. This applies to the built-in Slack and Discord connectors,
not arbitrary MCP OAuth servers. Local auth and `oauth.callback_base_uri` must be
configured to offer linking.

If profile verification or linking is unavailable after OAuth succeeds, the callback
says the connection is ready and directs you to request a profile link in the chat.

The ticket travels in the URL fragment, is removed from the address bar immediately,
and reaches the API only in a same-origin POST body. The database stores its SHA-256
digest, not the ticket. A new `oauth_link_profile` call invalidates the profile's previous
unused ticket. Text such as `/bind` has no special channel behavior.

Change your password in Trunkline's Account panel. `POST /api/auth/password` accepts
`current_password` and `password`, applying the same requirements as registration.
It requires the current password and signs out all of the account's browser sessions.
API keys remain valid and can be revoked separately in the Account panel.

For a forgotten password, an administrator can reset an existing account locally,
using the server's config home and database:

```sh
uv run octomate service user reset-password --username alice
```

The command prompts for the new password and confirmation without echoing them.
`--password` can also supply it directly. A reset ends all browser sessions; the
user then signs in through Trunkline with the new password.

## CLI and API tokens

Sign in through the Trunkline UI. Issue a token through the authenticated account
API below, then save it on the client machine:

```sh
octomate configure --url https://octomate.example.com --token '<api-token>'
```

The CLI saves the token in `~/.config/octomate/cli.toml` with mode 600; use
`--scope project` for `.octomate/cli.toml`. The configuration key is `token`, and
`OCTOMATE_CLI_TOKEN` overrides it. Native hook scripts and transcript streams
require `hooks`; installed MCP clients require `mcp`. Re-run each runtime's
`mcp install` after changing the token, because those installations embed their
credentials.

Authenticated account endpoints (using the browser session cookies) are:

- `POST /api/auth/api-keys`: issue with `name`, `scopes`, and optional timezone-aware
  `expires_at`. Returns `{key, token}`; this is the only disclosure of the token.
- `GET /api/auth/api-keys`: list your key metadata, including revoked keys; no tokens
  or hashes are returned.
- `DELETE /api/auth/api-keys/{id}`: revoke your key. Other users' keys return 404.

Writes require `X-Octomate-Request: 1`. Keys have no expiry unless `expires_at` is
specified. Revocation applies to new authentication checks on hooks, streams, and
MCP. Password access and refresh tokens cannot substitute for API tokens.

Driven Codex clients receive a temporary `mcp` key for the kicking user. It expires
after `auth.runtime_api_key_lifetime` (default `P1D`) and is revoked when its client
closes. The key's plaintext stays with that running client, never on the user row.

The migration removes `users.secret` and its values. Old client secrets are no
longer accepted; sign in again and reinstall MCP entries. Downgrading restores an
empty nullable column and cannot recover the old secrets.
