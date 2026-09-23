# Server setup

The shared bootstrap for Linux, WSL and a hand-built macOS install. On macOS the
[wizard](macos.md) writes most of this for you; come here for the database, the
console build and the first run.

## Keep the installation separate

One directory, owned by the account that runs the server and separate from any
checkout you develop in:

```text
<installation>/
  app/                     the release checkout and its .venv
  config/                  the server's YAML files, the config home
  .env                     secrets: auth salts, tokens, the OAuth key
  octomate.db              the database
  .octomate/               mirrors, workspaces, downloaded files
  control/                 the service definition and the operation lock
  backups/                 database snapshots
  logs/                    service output
```

Run the server and every maintenance command with the installation as the working
directory. `.env`, `.octomate/` and the default database path all resolve there.
`OCTOMATE_HOME` names `config/`; it moves nothing else.

The guides use `$OCTOMATE_INSTALL_ROOT` for this directory. It is a convention of
the documentation, not a variable the CLI reads.

## Create the installation

Pick a server tag from [Releases](https://github.com/kalynnka/octomate/releases),
one of the `octomate-vX.Y.Z` tags rather than a CLI or protocol tag:

```sh
export OCTOMATE_INSTALL_ROOT="$HOME/.local/share/octomate-server"
mkdir -p "$OCTOMATE_INSTALL_ROOT" && chmod 700 "$OCTOMATE_INSTALL_ROOT"
cd "$OCTOMATE_INSTALL_ROOT"
mkdir config control backups logs
git clone --branch 'octomate-vX.Y.Z' --depth 1 https://github.com/kalynnka/octomate.git app
uv sync --locked --no-dev --project "$OCTOMATE_INSTALL_ROOT/app" --python 3.13
```

The checkout includes the `cli/` and `protocol/` workspace packages; keep it whole.

`uv tool install octomate` installs the server from PyPI instead. It runs
`octomate service serve` but ships no console build and no source checkout, which
the macOS managed upgrade needs. Prefer the checkout.

## Set the service context

In every shell you use for setup:

```sh
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
cd "$OCTOMATE_INSTALL_ROOT"
```

An absolute path makes four slashes after the scheme. Note the single underscore in
`OCTOMATE_DB_URL`; the nested `OCTOMATE__` prefix is for settings.

## Configure one agent and the console

`config/octomate.yaml`:

```yaml
host: 127.0.0.1
port: 8000
```

`config/tentacles.yaml`, with `type: codex` instead if that is the harness you use:

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default
  trunkline:
    type: trunkline
    agents: [claude]
```

Log the harness in as the account that runs the server. It supplies its own model
catalog and default; nothing else is needed for a harness agent.

Generate the three auth salts into `.env` without printing them. The `x` mode refuses
to overwrite an existing file:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/python" - <<'PY'
import os, secrets
from pathlib import Path
fd = os.open(Path(".env"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    for name in ("ACCESS_TOKEN_SALT", "REFRESH_TOKEN_SALT", "API_KEY_SALT"):
        f.write(f"OCTOMATE__AUTH__{name}={secrets.token_urlsafe(32)}\n")
PY
```

Declaring `auth:` requires all three, and `.env` supplies them, so `config/auth.yaml`
only needs what differs from the defaults. For the first run over loopback HTTP:

```yaml
auth:
  cookie_secure: false
```

Set it back to `true` once you serve through HTTPS. See
[Configuration](configuration.md) for everything else, including the read-only
validation snippet that prints the resolved database path.

## Initialize the database

!!! warning "This writes the database the environment names"
    Check the printed path first. For an existing installation, stop the server
    and take a snapshot; a code rollback does not reverse a migration.

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" upgrade head
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" current
```

Starting the server never migrates. Every later release needs this step again; see
[Upgrades and backups](upgrading.md).

## Build Trunkline

The console is the shortest path to registering and issuing a token.

```sh
cd "$OCTOMATE_INSTALL_ROOT/app/trunkline"
pnpm install --frozen-lockfile
pnpm build
cd "$OCTOMATE_INSTALL_ROOT"
```

Then add `static_dir` to the `trunkline` block, as an absolute path. YAML does not
expand shell variables:

```yaml
  trunkline:
    type: trunkline
    agents: [claude]
    static_dir: /home/alice/.local/share/octomate-server/app/trunkline/dist
```

The directory must exist when the server starts. Without `static_dir` the console
API is still served and the frontend is not.

## Run and verify

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service serve
```

Open `http://127.0.0.1:8000`. Then check that the protected surfaces refuse an
anonymous caller:

```sh
curl -i http://127.0.0.1:8000/api/trunkline/health
curl -i -X POST http://127.0.0.1:8000/octomate/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -i -X POST http://127.0.0.1:8000/hooks/claude -H 'Content-Type: application/json' -d '{}'
```

Each should answer **401**. A **503** from the console means no `auth:` block; a
**404** means that route is not mounted, which for a hook route means the agent
tentacle is not enabled. A listening port proves nothing about channels: read the
startup log for each tentacle's connection line.

Continue with [Accounts and tokens](accounts.md), then [Hooks and
MCP](clients/index.md).
Stop this foreground process before starting a [macOS](macos.md#activate-the-gui-
service)
or [Linux](linux.md#install-a-systemd-user-service) service on the same port.
