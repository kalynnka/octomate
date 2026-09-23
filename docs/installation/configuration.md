# Configuration

The **config home** is the directory containing your server's YAML files. The
installation guides set `OCTOMATE_HOME=<installation>/config` so the server and
maintenance commands read the same files.

For the first run, review `octomate.yaml` (address), `auth.yaml` (sign-in) and
`tentacles.yaml` (agent and console). Supply secrets in YAML or environment
variables; a `.env` file is optional.
[Manual setup](server.md) walks through these files in order; this page explains
how Octomate finds and combines them.

## The config home

Octomate chooses one directory. It never merges several homes.

<div class="config-home-table" markdown>

| | Directory | When it wins |
|---|---|---|
| 1 | `$OCTOMATE_HOME` | The variable is set and non-empty. Used as given, even if the directory is empty or missing. `~` is expanded. |
| 2 | `./.octomate/config/` | It holds at least one of the seven files below. |
| 3 | `~/.octomate/config/` | Otherwise. Named even when it does not exist, so a boot error can say where it looked. |

</div>

An empty `OCTOMATE_HOME` counts as unset. The `config/` subdirectory is what marks a
directory as server configuration: `.octomate/` itself holds the database, the
client's `cli.toml`, mirrors and workspaces, none of which make it a config home.

## The seven files

| File | Top-level keys | Covered in |
|---|---|---|
| `octomate.yaml` | `host`, `port`, `db_url` | [Server settings](settings.md) |
| `tentacles.yaml` | `tentacles` | [The tentacle registry](tentacles.md) |
| `auth.yaml` | `auth` | [Accounts and tokens](accounts.md) |
| `projects.yaml` | `projects` | [Register projects](projects.md) |
| `providers.yaml` | `providers` | [Inkling](../usage/agents/inkling.md) |
| `observability.yaml` | `logging`, `logfire` | [Observability](observability.md) |
| `oauth.yaml` | `oauth` | [OAuth settings](settings.md#profile-linking-and-mcp-authorisation) |

The assignment of keys to files is a convention. Every file is read into one
settings payload, so a key placed in the wrong file still applies. Keep the
convention anyway: it is what lets a reader find a setting.

Missing files are fine. The repository ships commented YAML templates in
[`octomate/config/defaults/`](https://github.com/kalynnka/octomate/tree/main/octomate/config/defaults),
one file per name, entirely commented. They document every key's built-in default
and set nothing, so nothing is enabled and no model is chosen for you.
[Manual setup](server.md#or-start-from-the-templates) shows how to copy them into
a new config home. Uncomment only the blocks you need and replace placeholders.

Layering is per top-level key and wholesale. A home that declares `tentacles:`
replaces the packaged `tentacles:` entirely rather than merging into it, and
inherits every other key. Within one key, a YAML block that sets one nested field
keeps the defaults of its siblings: a `stream:` block naming only `flush_interval`
still has the channel's default `min_chars`.

## Precedence and secrets

Strongest first:

1. Values passed in Python when the settings object is built. Tests use this.
2. The process environment.
3. YAML from the config home, over the packaged defaults.
4. `.env` in the server's **working directory**.
5. Field defaults.

Environment names use the `OCTOMATE__` prefix with `__` between nested keys:

```dotenv
OCTOMATE__TENTACLES__SLACK__BOT_TOKEN=xoxb-...
OCTOMATE__TENTACLES__SLACK__APP_TOKEN=xapp-...
OCTOMATE__OAUTH__ENCRYPTION_KEY=...
OCTOMATE__AUTH__ACCESS_TOKEN_SALT=...
```

The path after the prefix follows the YAML structure, so the tentacle key you chose
is part of the name: a channel declared as `lark_ops:` reads
`OCTOMATE__TENTACLES__LARK_OPS__APP_SECRET`.

!!! warning "YAML beats `.env`"
    A value in the config home overrides the same value in `.env`. When you move a
    secret into `.env`, delete the YAML field. A `FILL_IN_...` placeholder left in
    YAML silently wins over the real credential.

Secrets can live in the corresponding YAML fields or in the service environment.
Choose the source that suits your deployment and keep credentials out of shared
examples and version control. The generator uses `.env` for convenience; it is
not required. If used, `.env` is read from the server's working directory.
Secret fields use Pydantic's redaction, but the underlying files still contain
the original values.

YAML does not interpolate `$VARIABLE`. Path fields accept `~`. There is no live
reload: restart the server after editing configuration.

## Three similarly named settings

| Name | Meaning |
|---|---|
| `OCTOMATE_HOME` | The directory holding the server's YAML files |
| `OCTOMATE_DB_URL` | The database, one underscore |
| `OCTOMATE_CLI_URL`, `OCTOMATE_CLI_TOKEN` | The **client's** server address and API token, unrelated to server configuration |

`~/.config/octomate/cli.toml` is client configuration and is never read by the
server. Setting a server salt does not issue a client token. See
[Hooks and MCP](clients/index.md) for the client side.

## Validate without starting

From the installation root, use the configuration check already provided by the
CLI package. Set the context as in [Manual setup](server.md#set-the-service-context),
then inspect the target URL and check the settings:

```sh
cd "$OCTOMATE_INSTALL_ROOT"
printf '%s\n' "$OCTOMATE_DB_URL"
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/python" -m octomate_cli.deployment check
```

This internal CLI entry point validates settings, requires an explicit absolute
SQLite URL and checks the bind address. It succeeds silently, opens no database
connection and starts no tentacle. Confirm the printed URL points to the intended
installation before running a migration. It does not test credentials, initialise
the OAuth cipher or verify the database schema.
