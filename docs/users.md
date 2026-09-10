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
seed users or change profile bindings. User records hold no plaintext bearer.
Hooks and MCP authenticate separate, named API keys stored only as salted hashes.

Trunkline shows threads in which one of the signed-in user's bound profiles has
spoken, including linked IM and native client history. The same access check covers
messages, conversations, projects, pending actions, permission changes, and action
responses. Console chat identities use the user's stable ID. Old console history
under the former `dev` profile remains stored and hidden until it is bound to an
owner. The channel account connection and binding flow will be designed separately.

Account endpoints are `/api/auth/register`, `/login`, `/refresh`, `/logout`, and
`/me` under the same `/api/auth` prefix. They use browser cookies; passwords and
session tokens are excluded from response bodies. Channel-initiated binding and
MCP OAuth are separate follow-up work.

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
