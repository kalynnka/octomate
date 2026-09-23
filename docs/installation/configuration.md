# Configuration

A deployment is a **config home**: one directory holding one flat YAML file per
subsystem. Each file's top-level keys are settings field names, so the files add up
to one settings payload with no wrapper key. Changing a channel touches
`tentacles.yaml` and nothing else.

## The config home

Octomate chooses one directory. It never merges several homes.

| | Directory | When it wins |
|---|---|---|
| 1 | `$OCTOMATE_HOME` | The variable is set and non-empty. Used as given, even if the directory is empty or missing. `~` is expanded. |
| 2 | `./.octomate/config/` | It holds at least one of the seven files below. |
| 3 | `~/.octomate/config/` | Otherwise. Named even when it does not exist, so a boot error can say where it looked. |

An empty `OCTOMATE_HOME` counts as unset. The `config/` subdirectory is what marks a
directory as server configuration: `.octomate/` itself holds the database, the
client's `cli.toml`, mirrors and workspaces, none of which make it a config home.

## The seven files

| File | Top-level keys | Covered in |
|---|---|---|
| `octomate.yaml` | `host`, `port`, `db_url` | [Server settings](settings.md) |
| `tentacles.yaml` | `tentacles` | [The tentacle registry](tentacles.md) |
| `auth.yaml` | `auth` | [Accounts and tokens](accounts.md) |
| `projects.yaml` | `projects` | [Projects](../usage/projects.md) |
| `providers.yaml` | `providers` | [Inkling](../usage/agents/inkling.md) |
| `observability.yaml` | `logging`, `logfire` | [Observability](observability.md) |
| `oauth.yaml` | `oauth` | [MCP proxy](../usage/mcp/proxy.md) |

The assignment of keys to files is a convention. Every file is read into one
settings payload, so a key placed in the wrong file still applies. Keep the
convention anyway: it is what lets a reader find a setting.

Missing files are fine. Beneath the home sit the packaged defaults in
[`octomate/config/defaults/`](https://github.com/kalynnka/octomate/tree/main/octomate/config/defaults),
one file per name, entirely commented. They document every key's built-in default
and set nothing, so nothing is enabled and no model is chosen for you.

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

Keep secrets out of the home. Every credential field is a `SecretStr`, so it is
redacted from logs and error messages, but the home is a directory people copy and
share. `.env` is read relative to the working directory the server starts in, which
is why every guide runs the server from the installation root.

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

This checks the settings from the intended working directory and prints only the
resolved database path. It opens no connection and starts no tentacle:

```sh
cd "$OCTOMATE_INSTALL_ROOT"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/python" - <<'PY'
from pathlib import Path
from sqlalchemy.engine import make_url
from octomate.config import OctomateConfig
from octomate.config.database import database_settings

config = OctomateConfig()
url = make_url(database_settings.db_url)
print("tentacles:", ", ".join(f"{k} ({v.type})" for k, v in config.tentacles.items()))
print("database:", Path(url.database).resolve(), "(exists)" if Path(url.database).exists() else "(new)")
PY
```

A validation error names the offending key. Credentials are hidden from the message;
the `projects:` block is the one exception, because its usual mistake is a list where
a mapping keyed by name belongs, and the message needs to show it.
