# Server settings

Review these after preparing the server. Start with the database path and local
address, then browser sign-in. Add OAuth settings when you connect chat identities
or MCP services. [Configuration](configuration.md) explains file locations and
precedence.

## The database

The database URL is the one setting that lives outside the main settings object,
because migrations must find the database without validating a whole deployment.

| Source, strongest first | Value |
|---|---|
| `OCTOMATE_DB_URL` | **One** underscore before `DB`, unlike the `OCTOMATE__` prefix. |
| `db_url:` in `octomate.yaml` | |
| `.env` | |
| Default | `sqlite+aiosqlite:///.octomate/octomate.db`, relative to the working directory |

For a deployment, set it to an absolute path. An absolute Unix path gives four
slashes: `sqlite+aiosqlite:////srv/octomate/octomate.db`. The guides set it explicitly
in the service environment. Run the
[configuration and target check](configuration.md#validate-without-starting) before a
migration; an Alembic command does not ask you to confirm its target.

## Host and port

```yaml
host: 127.0.0.1
port: 8000
```

Keep the default loopback bind. For access from other devices, use a private
connection such as [Tailscale Serve](networking.md#tailscale). Channel connections
dial out and do not require a public server address.

## Authentication and OAuth

### Browser sign-in

Trunkline sign-in, invitations and API tokens need three independent salts. The
wizard and its configuration generator create them in `.env`. You can instead
supply them in `auth.yaml` or the service environment:

| Variable | Protects |
|---|---|
| `OCTOMATE__AUTH__ACCESS_TOKEN_SALT` | Browser access tokens |
| `OCTOMATE__AUTH__REFRESH_TOKEN_SALT` | Browser refresh tokens |
| `OCTOMATE__AUTH__API_KEY_SALT` | Client API keys |

When starting from the YAML templates, run `openssl rand -base64 32` separately
for each salt. Save the results as `auth.access_token_salt`,
`auth.refresh_token_salt` and `auth.api_key_salt`, or their environment equivalents.
Keep these values stable and back them up with the database. A changed salt
invalidates credentials hashed with it. If using `.env`, remove matching YAML
placeholders because YAML takes precedence.

In `config/auth.yaml`, select the cookie setting for the URL your browser uses:

```yaml
auth:
  cookie_secure: false       # local HTTP setup; use true behind HTTPS
```

Durations such as `access_token_lifetime` accept ISO 8601 (`PT15M`, `P7D`) or bare
seconds. [Accounts and tokens](accounts.md) covers what each one governs.

### Profile linking and MCP authorisation

You can leave OAuth unconfigured for a local agent and Trunkline. When you need
it, the `oauth:` block serves two purposes: **profile linking**, which needs
`callback_base_uri`, and **stored credentials**, which need `encryption_key`.

In `config/oauth.yaml`:

```yaml
oauth:
  callback_base_uri: https://octomate.example.com   # origin only, no path
  authorization_lifetime: PT10M
```

Set `oauth.encryption_key` in YAML or `OCTOMATE__OAUTH__ENCRYPTION_KEY` in the
environment to a URL-safe base64 encoding of 32 random bytes. The wizard generates
one when an MCP preset is selected. For a new key, use the short command from the
packaged OAuth template:

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

Reuse an existing key when adding a connector. The loader requires it for enabled
`oauth` and `oauth_discovery` MCP templates, and for Slack or Discord channel
blocks that declare an OAuth client. Stored bearer tokens also need encryption.
An MCP using an authorization-code flow needs `callback_base_uri` as well.
Keep the key with the database backup: stored grants cannot be decrypted without it.

The current wizard's optional MCP preset writes a key without base64 padding,
which the OAuth cipher rejects. In a new installation, before any credentials
have been stored, replace that generated value with the padded output above.
For an existing key, preserve its decoded bytes; do not generate a replacement
to fix its formatting. The configuration check does not validate the cipher key;
verify an actual connector authorisation after configuration.

## Everything else

| Key | Purpose | Defaults |
|---|---|---|
| `projects` | Code locations an agent may work in, keyed by name. Each needs a `root` and an `upstream`. | [Register projects](projects.md) |
| `providers` | API keys and per-provider model settings for Inkling. Omitted providers fall back to their native environment variables. | [Inkling](../usage/agents/inkling.md) |
| `mirrors` | How project mirrors are synced: `freshness_window` (0 s) and the git `identity` Octomate commits with. | [Workspaces](../concepts/workspaces.md) |
| `workspaces` | When idle workspaces are reclaimed: `idle_window` (24 h), `sweep_interval` (1 h). | [Workspaces](../concepts/workspaces.md) |
| `mcp_pool` | `idle_timeout` (1 h) before a cached vendor MCP client is closed. | [MCP proxy](../usage/mcp/proxy.md) |
| `logging`, `logfire` | Log level, per-logger overrides, Logfire export and per-library instrumentation. | [Observability](observability.md) |

The [settings reference](../api/config.md) lists every field with its type,
default and description, generated from the source.
