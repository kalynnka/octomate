# Upgrades and backups

Two things upgrade separately, and the CLI keeps them apart on purpose.

| Command | Upgrades | Leaves alone |
|---|---|---|
| `octomate upgrade` | The standalone operator CLI, through `uv tool upgrade` | The server, its environment, its database |
| `octomate service upgrade` | A macOS GUI service: release, dependencies, database | The operator CLI |

`octomate upgrade` succeeds only for a `uv tool install octomate-cli` installation.
Run from the server's own environment it refuses and names the standalone install;
run through `uvx` it tells you how to refresh that invocation. A compatible server
release never requires a client upgrade: the transcript stream checks a wire protocol
version at the handshake, not package versions, and refuses a mismatch with a clear
line.

## What to back up

- The database, with a consistent snapshot rather than a file copy. A copied `.db`
  can miss transactions still in the write-ahead log. Use SQLite's backup API:
  `sqlite3 octomate.db ".backup '/backups/octomate-$(date +%F).sqlite3'"`.
- The config home and `.env`. The `auth` salts and `oauth.encryption_key` are the
  part you cannot regenerate: without the salts every session and API key is
  invalid, and without the key every stored OAuth token is unreadable.
- Optionally `.octomate/mirrors/`. Mirrors are rebuilt from their upstreams, but a
  thread's saved snapshots live under `refs/octomate/threads/` in the mirror and
  are lost with it.

Workspaces under `.octomate/workspaces/` are caches; every turn's work is in the
mirror.

## Migrations

`octomate service serve`, `service start` and `service restart` never migrate. A
start on a stale schema refuses. Migrations are a deliberate step:

```sh
cd "$OCTOMATE_INSTALL_ROOT"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" upgrade head
```

The migration environment resolves the database the same way the server does, so
`OCTOMATE_DB_URL` is what points it at a copy. Rehearse first: copy the backup to a
scratch path, run the same command against it, then check `PRAGMA integrity_check`
and `PRAGMA foreign_key_check`. A downgrade does not always undo an upgrade, and a
code rollback never reverses a data migration.

## macOS managed upgrade

`octomate service upgrade`, run from the standalone CLI:

1. Validates the plist, the configuration and the database target, and refuses a
   checkout with tracked local changes.
2. Finds the highest stable `octomate-vX.Y.Z` release on GitHub and fetches its tag.
   Already there means it returns without stopping anything. A release that does
   not contain the installed commit is refused as a downgrade or divergence.
3. Stops the service and waits for its process group and its port.
4. Takes a consistent snapshot into `backups/`.
5. Checks out the release and syncs its locked dependencies.
6. Rehearses the pending migrations on a copy of the snapshot, checks the copy's
   revision, integrity and foreign keys, and only then migrates the real database.
7. Starts the service, waits for the listener, and verifies that `/octomate/mcp`
   and the console routes are protected.

A failure after the service stopped leaves it disabled and prints the phase, the
backup path and the next command. Nothing is retried or rolled back automatically.
Every step is appended to `logs/server.log`.

The web console is not part of it. After a server upgrade, rebuild Trunkline from
the new checkout as in [Server setup](server.md#build-trunkline).

## Linux and Docker

By hand, in the same order the managed upgrade takes:

```sh
systemctl --user stop octomate.service
sqlite3 "$OCTOMATE_INSTALL_ROOT/octomate.db" ".backup '$OCTOMATE_INSTALL_ROOT/backups/pre-upgrade.sqlite3'"
git -C "$OCTOMATE_INSTALL_ROOT/app" fetch --tags
git -C "$OCTOMATE_INSTALL_ROOT/app" checkout --detach 'octomate-vX.Y.Z'
uv sync --locked --no-dev --project "$OCTOMATE_INSTALL_ROOT/app"
# rehearse on a copy, then:
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" upgrade head
systemctl --user start octomate.service
```

Docker follows the same shape with `docker compose`; see [Docker](docker.md#upgrading).
