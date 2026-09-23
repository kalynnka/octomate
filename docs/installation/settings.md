# Server settings

The settings a deployment sets by hand: the database, the bind address, the salts and
keys behind sign-in and stored credentials, and where the remaining keys are
explained. [Configuration](configuration.md) covers where the files live and how they
layer.

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
in the service environment, and the maintenance commands print the resolved path
before they touch it.

## Host and port

```yaml
host: 127.0.0.1
port: 8000
```

The default binds loopback. Inside a container that is reachable from nowhere, so
`docker-compose.yml` sets `OCTOMATE__HOST=0.0.0.0`. Every channel dials out, so a
non-loopback bind is only needed for what you point at Octomate yourself: the
console, native-session hooks from other machines, and OAuth callbacks. See
[Networking](networking.md).

## Authentication and OAuth

Trunkline sign-in, invitations and API tokens need an `auth:` block with three
independent salts. Generate each one separately and keep them stable: rotating a
salt invalidates every credential hashed with it.

```yaml
auth:
  access_token_salt: ...      # each: openssl rand -base64 32
  refresh_token_salt: ...
  api_key_salt: ...
  cookie_secure: true         # false only for loopback HTTP
```

Durations such as `access_token_lifetime` accept ISO 8601 (`PT15M`, `P7D`) or bare
seconds. [Accounts and tokens](accounts.md) covers what each one governs.

The `oauth:` block serves two things: **profile linking**, which needs
`callback_base_uri`, and **stored credentials**, which need `encryption_key`.

```yaml
oauth:
  callback_base_uri: https://octomate.example.com   # origin only, no path
  encryption_key: ...        # openssl rand -base64 32 | tr '+/' '-_'
  authorization_lifetime: PT10M
```

The loader requires `encryption_key` as soon as any enabled tentacle stores a
credential: an `oauth` or `oauth_discovery` MCP, or a Slack or Discord channel with
an `oauth` block. It requires `callback_base_uri` for any MCP that uses an
authorization-code flow. Keep the key with your database backups: encrypted tokens
are unreadable without it.

## Everything else

| Key | Purpose | Defaults |
|---|---|---|
| `projects` | Code locations an agent may work in, keyed by name. Each needs a `root` and an `upstream`. | [Projects](../usage/projects.md) |
| `providers` | API keys and per-provider model settings for Inkling. Omitted providers fall back to their native environment variables. | [Inkling](../usage/agents/inkling.md) |
| `mirrors` | How project mirrors are synced: `freshness_window` (0 s) and the git `identity` Octomate commits with. | [Workspaces](../concepts/workspaces.md) |
| `workspaces` | When idle workspaces are reclaimed: `idle_window` (24 h), `sweep_interval` (1 h). | [Workspaces](../concepts/workspaces.md) |
| `mcp_pool` | `idle_timeout` (1 h) before a cached vendor MCP client is closed. | [MCP proxy](../usage/mcp/proxy.md) |
| `logging`, `logfire` | Log level, per-logger overrides, Logfire export and per-library instrumentation. | [Observability](observability.md) |

The [settings reference](../api/config.md) lists every field with its type,
default and description, generated from the source.
