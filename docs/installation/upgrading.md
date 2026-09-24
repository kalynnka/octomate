# Upgrades and backups

The operator CLI and the server upgrade separately. Choose the command for the
component you intend to change, and back up the server before upgrading it.

| Command | Upgrades | Leaves alone |
|---|---|---|
| `octomate upgrade` | The standalone operator CLI, through `uv tool upgrade` | The server, its environment, its database |
| `octomate service upgrade` | A macOS GUI service: release, dependencies, database | The operator CLI |

`octomate upgrade` succeeds only for a `uv tool install octomate-cli` installation.
Run from the server's own environment it refuses and names the standalone install;
run through `uvx` it tells you how to refresh that invocation. The transcript stream
checks a wire protocol version at the handshake and reports incompatibility;
client and server package version numbers do not need to be identical.

## What to back up

- The database, with a consistent snapshot rather than a file copy. A copied `.db`
  can miss transactions still in the write-ahead log. Use SQLite's backup API:
  `sqlite3 octomate.db ".backup '/backups/octomate-$(date +%F).sqlite3'"`.
- The config home and secrets, wherever you supply them: YAML, the service
  environment or an optional `.env`. The `auth` salts and `oauth.encryption_key` are the
  part you cannot regenerate: without the salts every session and API key is
  invalid, and without the key every stored OAuth token is unreadable.
- `.octomate/mirrors/`, if you want to retain saved project work. An upstream can
  rebuild the original project, but a
  thread's saved snapshots live under `refs/octomate/threads/` in the mirror and
  are lost with it.

Retain active workspaces too when backing up work in progress. A failed save can
leave changes only in `.octomate/workspaces/`; those directories are not always
safe to discard. Quiesce agent work before taking a filesystem snapshot.

## Migrations

`octomate service serve`, `service start` and `service restart` never migrate.
Managed macOS startup checks that the schema is current; a foreground invocation
does not replace that check. First run the
[read-only path check](configuration.md#validate-without-starting). After backing
up and rehearsing an upgrade, migrate deliberately from the installation root:

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

Service stop, restart and upgrade wait up to 30 seconds for the old process group
to disappear, including the brief macOS permission error while exited processes
await reaping. If the group remains, the command fails and leaves the service
disabled instead of starting another instance.

The web console is not part of it. After a server upgrade, rebuild Trunkline from
the new checkout as in [Manual setup](server.md#build-trunkline).

## Manual installations

Stop the server using the supervisor you configured, or stop its foreground
process. Set the [service context](server.md#set-the-service-context), then take
a consistent database backup:

```sh
mkdir -p "$OCTOMATE_INSTALL_ROOT/backups"
export OCTOMATE_BACKUP="$OCTOMATE_INSTALL_ROOT/backups/pre-upgrade-$(date +%Y%m%d-%H%M%S).sqlite3"
sqlite3 "$OCTOMATE_INSTALL_ROOT/octomate.db" ".backup '$OCTOMATE_BACKUP'"
```

Back up the configuration and working data described above too. Review the target
release and local changes before checking out its tag:

```sh
git -C "$OCTOMATE_INSTALL_ROOT/app" fetch --tags
git -C "$OCTOMATE_INSTALL_ROOT/app" checkout --detach 'octomate-vX.Y.Z'
uv sync --locked --no-default-groups --project "$OCTOMATE_INSTALL_ROOT/app"
```

Rehearse migrations on a separate copy of the snapshot, with `OCTOMATE_DB_URL`
pointing explicitly to that copy. Confirm its path before running Alembic. A
successful rehearsal reaches the expected revision, reports `ok` from
`PRAGMA integrity_check`, and no rows from `PRAGMA foreign_key_check`.

After a successful rehearsal, restore the real service context above, run the
[path check](configuration.md#validate-without-starting) again, and migrate the
real database using the [migration command above](#migrations). Then
[rebuild Trunkline](server.md#build-trunkline), restart the server with the same
account and environment, and repeat the installation's HTTP, sign-in and agent checks.
