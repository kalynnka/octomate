# Accounts and tokens

Three credentials serve three purposes:

- Your **Octomate account** owns your linked channel profiles, your history, your
  API tokens and your installed MCP connectors.
- Your **harness login** lets Claude Code, Codex or dsh call its provider. Octomate
  never sees it.
- A **bot token** connects the server to a chat platform. It belongs to the
  deployment, not to a person.

## Register the first account

Registration is by invitation. Issue one from the server's own environment, with the
same config home and database the server uses:

```sh
cd "$OCTOMATE_INSTALL_ROOT"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service invite --url http://127.0.0.1:8000
```

That prints a registration link. Open it in the browser, choose a username, a display
name and a password. The invitation token travels in the URL fragment, so it never
reaches a request log, and only its hash is stored. One invitation admits one
account and expires after seven days. It reserves nothing: anyone holding the link
can use it.

Passwords need 11 to 1024 characters with at least one lowercase letter, one
uppercase letter, one digit and one symbol. Whitespace is not a symbol.

Without `--url`, `service invite` prints a bare code for the CLI path:

```sh
octomate service user create --username alice --invitationcode '<code>' --password '...'
```

Prefer the browser: it keeps the password out of shell history. Both paths need the
server package and an `auth:` block in the config home. Registration issues no
token; sign in to Trunkline next.

## Issue a client token

In Trunkline, open **Account** and issue an API key named for the client machine.
Choose its scopes:

| Scope | Used by |
|---|---|
| `hooks` | The hook handlers and the transcript stream |
| `mcp` | The `/octomate/mcp` endpoint |

One key may carry both. The token is shown once. Save it on the client:

```sh
octomate configure --url http://127.0.0.1:8000 --token '<api-token>'
```

Use the HTTPS origin for a remote client. `configure` stores a token you already
have; it creates nothing.

Keys have no expiry unless you set one. Revoking a key applies to the next request
that presents it. The account API behind the panel:

| Endpoint | Contract |
|---|---|
| `POST /api/auth/api-keys` | `name`, `scopes`, optional `expires_at`. Returns the token once. |
| `GET /api/auth/api-keys` | Your keys, revoked ones included, never the tokens. |
| `DELETE /api/auth/api-keys/{id}` | Revoke. Another user's key is a 404. |

These use the browser session cookie and need `X-Octomate-Request: 1` on writes.
The full schema is at the server's `/docs`.

## Link your channel profiles

The first time you speak to the bot on Slack, Lark, Discord or QQ, Octomate records
a **visitor profile**: a platform identity it has seen, owned by nobody. Linking
that profile to your account is what lets cross-channel routing, history search and
your installed MCP connectors recognise you. Until then the agent answers, but knows
nothing beyond the current chat.

Two ways to link:

- **From the chat.** Ask the agent to link your profile. It calls
  `oauth_link_profile`, which sends a one-time link to your direct messages on that
  platform, never to the group. Open it, sign in if needed, review the profile and
  the account shown, and confirm. A profile owned by someone else is never
  transferred.
- **From Trunkline.** Under **Profile**, connect Slack or Discord with the
  platform's own OAuth. This needs the channel's `oauth` block configured. Lark and
  QQ link from the chat only.

Both need `auth` and `oauth.callback_base_uri` configured. Linking a profile does not
authorise any MCP connector; that is a separate consent per connector.

Trunkline needs no linking. It is your signed-in account already, which is also why
its profile cannot be unlinked.

## Rotation and recovery

- **Lost or leaked token.** Revoke it in the Account panel, issue a replacement,
  run `configure` again, and reinstall every MCP entry. Hooks resolve the new token
  on their own.
- **Password change.** Signs out every browser session. API keys stay valid.
- **Forgotten password.** An operator resets it locally, with the same environment
  as the invite command above:

    ```sh
    "$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service user reset-password --username alice
    ```

    It prompts without echo and signs out the account's browser sessions.

- **Salts.** The three `auth` salts hash every session, refresh token and API key.
  Keep them in your backups. Rotating one signs everyone out or revokes every key;
  it is not how you rotate one client's token.

## Sign-in checks

- A **503** from the console means no `auth:` block is configured.
- Signing in succeeds but the page returns to the login form on the next request:
  over plain HTTP, set `auth.cookie_secure: false`. Keep it `true` behind HTTPS.
- Access tokens last 15 minutes, sessions 15 days. Refresh rotates both credentials
  without extending the deadline.
- Session cookies are scoped to `/api`, `HttpOnly` and `SameSite=Strict`. A browser
  write without `X-Octomate-Request: 1` is a 403, which is why the console and the
  API must share an origin.
