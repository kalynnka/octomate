# FastAPI server deployment

This guide covers installing Octomate's FastAPI server, managing it through the CLI,
and optionally exposing its API with a separate Tailcat setup.

## Scope and success criteria

Install and run Octomate natively on the server, listening on `127.0.0.1:8000`.
The operator uses `octomate serve --plist <path>` and `octomate upgrade`.
Both commands apply pending Alembic migrations before starting the service;
upgrade also installs the latest stable release and syncs its locked dependencies.

Tailcat is a separate networking step after the local installation works. It is
the chosen way to expose Octomate to remote clients, with its own setup and lifecycle.
Octomate installation, start, upgrade and verification work without Tailcat installed
or running. There is no version selection, shell deployment script or scheduled update.

Octomate installation is complete when:

- Local CLI, hooks, transcript streams and gateway MCP work against
  `127.0.0.1:8000` with a registered user's bearer; missing or invalid bearers
  cannot use hooks or MCP.
- Trunkline and its control API are disabled in production configuration.
  No frontend is built or served; UI user validation is future work.
- The application listens only on the server's loopback interface.
- CLI start and upgrade apply pending migrations and start the service successfully.
  Upgrade installs the latest stable release; a failed migration prevents service
  startup and exits with a clear error and backup location.
- The service survives SSH disconnection and recovers from process failure.
  Reboot recovery is demonstrated.

The extra expose/connect step has its own checks in section 8: approved devices can
connect through Tailcat, unapproved devices cannot, and existing VPN or proxy software
continues working with its configured settings.

## 1. Prerequisites

- An unprivileged service account and a prepared service definition for the CLI's
  current launchd adapter, described in section 5.
- Git, uv and the project's Python version available through explicit executable
  paths or the service's configured PATH.
- Access to the source repository and locked dependencies.
- A dedicated deployment checkout, separate from development checkouts.
- A designated production database and configuration, with credentials stored outside Git.

Verify these prerequisites in the environment where the server will run.

## 2. Octomate API configuration

The application owns its routes and authentication. Enabled hooks and MCP retain
their existing bearer checks. Local clients connect to `http://127.0.0.1:8000`.

Disable or omit every `type: trunkline` channel in production configuration,
regardless of its instance name, so its control API is not registered. Do not enable
another browser-facing channel or build/serve the frontend. Validate the effective
configuration, including environment overrides. Add user validation to the UI and
its backing API in a later change before enabling the production console.

FastAPI's `/docs` and `/openapi.json` remain available on the local application
port. OAuth routes follow the application's existing connector registration.

Keep `mcp_path: /mcp`, matching the CLI's existing gateway URL. Current ownership
is in [application assembly](../octomate/base.py),
[hook authentication](../octomate/tentacles/hooks.py) and the
[CLI gateway address](../cli/octomate_cli/tentacles/mcp.py).

Authorization-code OAuth browser flows remain outside the first rollout. Leave
integrations requiring those callbacks disabled unless an existing authorized
connection is deliberately supported. Device authorization flows do not require
an incoming browser callback here.

## 3. Production directories and credentials

Create a new deployment tree without modifying existing development checkouts:

```text
/absolute/path/to/octomate/
  app/                     one Git checkout at a release, including cli/, protocol/, .venv/
  shared/config/           production server YAML
  shared/.env              production secrets
  shared/octomate.db       designated production database
  shared/mirrors/          persistent project mirrors
  shared/workspaces/       persistent agent workspaces
  control/                 service definitions and operation lock
  backups/                 consistent database snapshots
  logs/                    application and update logs
```

Replace `/absolute/path/to/octomate` with the chosen deployment directory.
At bootstrap, clone the source repository into `app/` at the latest stable
GitHub release tag, with `origin` pointing to that repository. Keep the checkout
detached at its release commit; the upgrade command advances it to a newer release.
Link `app/.octomate` to `../shared` and `app/.env` to `../shared/.env`.
Keep production configuration and persistent state outside the checkout. Existing
code keeps derived state under `.octomate`; preserve that directory as a unit.

Set these explicit values in the service environment:

```text
OCTOMATE_HOME=/absolute/path/to/octomate/shared/config
OCTOMATE_DB_URL=sqlite+aiosqlite:////absolute/path/to/octomate/shared/octomate.db
```

`OCTOMATE_HOME` names the configuration directory. The database variable is
`OCTOMATE_DB_URL`, not `OCTOMATE__DB_URL`. Bind the backend explicitly to
`127.0.0.1:8000`. Keep credentials out of Git and command arguments; restrict secret
files to the service user. Log status and failures without bearer values or bodies.

At bootstrap, select either a new production database or a consistent copy of the
designated existing database. Preserve its corresponding OAuth encryption key.
Do not infer that the old development database is disposable. Database selection,
production agents/models, user registrations and any enabled IM channels are
operator-supplied values; the CLI commands must not guess them.

Use Python 3.13, matching `.python-version`; the packages also support Python 3.12.
Include both `cli/` and `protocol/`, which are uv workspace dependencies. Runtime
dependencies are installed with `uv sync --locked --no-dev`. The application factory
and Alembic resources ship inside the `octomate` package. Launch
`app/.venv/bin/octomate serve` with `app/` as the working directory.
[uv synchronization](https://docs.astral.sh/uv/concepts/projects/sync/).

There is no frontend build, Node/pnpm deployment step, Vite process, static-file
server, or Docker requirement. Provider CLIs may still have their own runtime
dependencies. Octomate-driven agents need project checkouts and working credentials
on the server; laptop transcript tailers continue running on the laptop.

## 4. Octomate service

| Job | Location/context | Contract |
| --- | --- | --- |
| Octomate | Supervised process, unprivileged service user | One backend worker; explicit binary, working directory, PATH and config; restart on failure. |

Use absolute executable/log paths and an explicit PATH; the service does not inherit
the interactive shell environment. Configure restart throttling and log rotation.
`octomate upgrade` disables and stops the backend before changing its code,
dependencies or database, so supervision cannot restart it mid-update.
Verify Octomate and its agent subprocesses have exited before proceeding.

The CLI owns start/upgrade orchestration and migrations. The service runs the
`octomate serve` foreground process and supervises it after the command exits.
Reboot/crash restarts use the installed, already migrated version; they do not pull
code or repeatedly attempt a failed migration. There is no deployment timer or
GitHub webhook.

Test provider authentication and filesystem permissions as the configured service
user. Credentials must remain available when the server restarts unattended.

## 5. Server CLI commands

The Typer CLI exposes two top-level server commands:

| Command | Behavior |
| --- | --- |
| `octomate serve` | Run the API in the foreground; supports `--host`, `--port`, `--reload` and `--tmux`. Does not update code or run migrations. |
| `octomate serve --plist <path>` | Check the configured deployment, back up and apply pending migrations, then start the service and verify it. Does not download code. |
| `octomate upgrade` | **Launchd/plist services only.** Find and fetch the latest stable release, stop the service, back up, install the release and locked dependencies, migrate, restart and verify. An already current checkout is unchanged. |

Run these commands on the server using the CLI installed in `app/.venv/`; bootstrap
makes that executable available on the operator's PATH.
`serve --plist <path>` and `upgrade` use the configured deployment directory and the
same production environment and service account as the managed process. These
commands require the server checkout and its environment. `--plist` cannot be
combined with `--host`, `--port`, `--reload`, `--tmux` or `--session`; set the service's
configuration in its definition instead.

The current CLI service adapter uses launchd. `serve` uses it only when `--plist`
is supplied. `upgrade` reads `/Library/LaunchDaemons/io.octomate.server.plist` by
default; `--plist <path>` selects a prepared job. Installation of that job is a separate
bootstrap step. It must set an absolute `WorkingDirectory`, `UserName`,
`ProgramArguments` to `["<checkout>/.venv/bin/octomate", "serve"]`, `KeepAlive: true`,
and explicit `PATH`, `OCTOMATE_HOME` and absolute `OCTOMATE_DB_URL` environment values.
Use the service user's account to run the CLI. It invokes sudo only for launchd
changes. The managed service requires SQLite, a loopback host, a registered bearer,
and disabled Trunkline. Operation results are written to `logs/server.log`.

These commands manage only Octomate, its dependencies, migrations and service.
They do not install or invoke Tailcat, manage tunnel keys or jobs, or depend on a
remote forward. All server verification targets `http://127.0.0.1:8000` directly.

### Start

1. Resolve and validate the deployment paths, production configuration, database
   target and installed service definition. Take the same exclusive operation lock
   used by upgrade. If the managed service is already loaded, verify it without
   migrating under it.
2. With the backend stopped, apply pending migrations using the backup and migration
   procedure in section 6. An already current database needs no migration.
3. Enable and load the backend job. Verify local MCP initialization and tool
   discovery with a registered bearer, and absent Trunkline routes. Inspect the
   service logs separately for agent/channel startup; the protocol check does not
   establish readiness of every background component.

### Upgrade

**This command only supports an installed launchd service defined by a plist.**
It cannot upgrade a foreground `octomate serve` process, a tmux session or a service
managed by another supervisor. Those deployments require a manual update procedure.

Install the service definition and server checkout described above before running
the command. Run as the plist's `UserName`, without sudo. Omitting `--plist` selects
the default installed file; that file must exist. The command does not install a
service or discover other running servers.

```bash
# Default installed service definition
octomate upgrade

# A different installed service definition
octomate upgrade --plist /absolute/path/to/server.plist
```

The default is `/Library/LaunchDaemons/io.octomate.server.plist`. The checkout named
by the plist must have no tracked local changes.

1. Acquire the operation lock and validate the deployment configuration. Resolve
   the latest non-draft, non-prerelease GitHub release and fetch its version tag
   from `origin`. An unavailable release or failed fetch leaves the service running.
2. Compare the fetched commit with the installed commit. If they match, report
   the release and exit without changing the service or database. Otherwise require
   the installed commit to be an ancestor of the release; refuse downgrades and
   divergent local history before stopping the service.
3. Disable and unload the backend job, wait for its listening port to close, and
   take a consistent database backup. Run updates during a quiet window: requests
   and streams can be interrupted.
4. Check out the fetched commit in detached HEAD mode, then run
   `uv sync --locked --no-dev` against `app/.venv/`. Apply pending migrations from
   that version, then start and verify the backend.
5. Record the release tag, previous and resulting commits, migration revision,
   backup location and outcome.

The orchestration is Python CLI code, invoking Git, uv, Alembic and launchctl directly
with argument lists. Maintenance runs in a fresh `app/.venv/bin/python` process
using the job's environment, so migrations load the updated code and dependencies.
The existing [CLI registration](../cli/octomate_cli/main.py) and
[foreground runner](../cli/octomate_cli/serve.py) define the integration points.

A failed backup, checkout, dependency sync or migration ends the command with a nonzero
exit code and leaves the backend disabled. If startup verification fails, disable
and unload the backend job and report the failed check. The disabled state persists
across reboot. After resolving the failure, run `serve --plist <path>` to migrate and enable
the service again; migration failures can require manual database recovery first.

The current application has no complete drain/readiness signal. Coordinate active
agent work before upgrading, then use startup logs and authenticated protocol checks
to confirm recovery. Do not use Trunkline's constant health response or promise
lossless hook delivery during the outage.

Release Please creates version PRs and GitHub releases; publishing remains separate
from deployment. See [release preparation and account setup](releases.md).
Merging a release PR never invokes this host's upgrade command.

## 6. Integrated migrations and recovery

Pending Alembic upgrades are part of both CLI commands. The operator does not run a
separate Alembic command or answer a second migration prompt during a normal start
or upgrade.

Before changing an existing database, retain a consistent backup and rehearse the
pending upgrade on a disposable copy of that backup. Verify the copy's resolved path
is distinct from production; check its resulting revision and data integrity.
A failed rehearsal stops before production migration. For a deliberately new, empty
production database, apply the initial migrations at the configured path.

The CLI then runs the equivalent of `alembic upgrade head` against the designated
production database while the backend is stopped, and verifies the resulting revision
before service startup. Use the installed package's absolute `migrations/alembic.ini` path and the same
database configuration as the app. [Alembic command interface](https://alembic.sqlalchemy.org/en/latest/api/commands.html).

Run copy and production migrations in separate fresh processes, each with an explicit
`OCTOMATE_DB_URL` set before importing server code. The current
[migration environment](../octomate/migrations/env.py) resolves the database from application
settings and overrides the URL in Alembic's configuration; changing only that
configuration would not redirect a rehearsal away from production.

A failed migration stops startup and reports the error, target revision and backup
location. Do not automatically retry migrations, stamp a revision, downgrade or restore
the database: a failure may have left partial changes. Recovery remains an operator
action. No database was accessed or migrated while writing this plan.

For a failed application upgrade with compatible persisted state, the operator can
restore the previous code revision recorded in the log, sync its locked dependencies
and restart. Resume normal upgrades after returning the checkout to `main` with a
fix available. Code rollback requires checking persisted-data compatibility even if
no schema migration ran; restoring a backup can lose subsequent user work.

Use SQLite's backup API/CLI backup facility for a consistent snapshot; a live copy
of only the `.db` file can omit WAL transactions. Back up Octomate configuration,
encryption material and required persistent files as well. Keep an encrypted copy
outside the server and rehearse restoration to a separate path. Confirm backup
destination and retention at bootstrap. [SQLite backup guidance](https://sqlite.org/backup.html).

## 7. Installation order and acceptance tests

Keep the work in separately reviewable stages; none implies a commit or deployment.
Complete these stages locally with Tailcat absent or stopped.

| Stage | Deliverable | Required evidence before moving on |
| --- | --- | --- |
| 1. Production configuration | Config example and route checks | Trunkline disabled and its routes absent; no frontend served; enabled hooks and MCP retain bearer authentication. |
| 2. Server process | Octomate service definition, state directories, credentials | Service-context startup, restart after process failure, log rotation and a loopback-only listener. |
| 3. Server CLI | `serve --plist <path>` and `upgrade`, including Alembic | Disposable-data tests: first start migrates; repeat start is safe; upgrade pulls main and migrates before restart; dirty checkout is refused; failed sync or migration prevents startup. |

Exercise enabled hooks and MCP at `http://127.0.0.1:8000` with missing, invalid and
valid bearers. Verify `/api/trunkline` and a real nested console action return 404
with Trunkline disabled, and that no frontend is served. API documentation remains
available on that local address.

Use the real CLI against a disposable local test deployment for a synthetic hook,
WebSocket hello/ingest/reconnect, and MCP initialization/tool discovery.
Exercise every enabled harness and confirm user attribution. Record expected
protocol-level responses rather than assuming all MCP methods return 200 or all
WebSocket denials use the same status.

From another LAN device, direct connections to the server's 8000 must fail.
Before unattended operation, test service recovery after a server restart.

## 8. Extra step: expose and connect with Tailcat

Begin after Octomate passes its local installation checks. Install and configure
Tailcat separately on the server and each remote client using Tailcat's own tools.

```text
Client                                       FastAPI server
CLI / hooks / MCP
  -> 127.0.0.1:18080
  -> Tailcat forward == encrypted tunnel ==> Tailcat serves port 8000
                                             -> Octomate 127.0.0.1:8000
```

The tunnel exposes every route the application registers on port 8000, including
`/docs` and `/openapi.json`, to approved devices. Hooks and MCP retain Octomate's
bearer checks. UI configuration remains as described in section 2.

### Expose and connect

Tailcat is Tailscale's own project. It requires no Tailscale account and does not
install system routes or DNS settings. Its free relays are rate-limited.
[Official project](https://tailscale.com/tailcat).

Install a pinned, tested Tailcat version on the server and each client. The following
are proposed commands, not executed setup. Each client generates its own identity:

```sh
tailcat genkey --client --key=client-default
```

The server creates a saved identity and uses the client's complete printed public key:

```sh
tailcat genkey --key=octomate --fixed-region
tailcat serve --key=octomate --allow='<client-public-key>' 8000
```

Each client substitutes the server's printed address:

```sh
tailcat forward '<server-address>' 18080:8000
```

Saved keys plus a fixed relay region keep the address stable across restarts.
Client commands use the saved `client-default` identity automatically. Add each
device's public key to the allowlist; private keys stay on their originating hosts.
[Key and forwarding reference](https://github.com/tailscale/tailcat/blob/main/README.md).

Keep Tailcat's configuration, keys and logs outside the Octomate installation.
Use the same Tailcat identity/config context for key generation and serving.
Back up the server's key material separately. Share the address privately; avoid
putting it in Git, CI output or public DNS. Record device ownership with its public key.

Configure the CLI after the forward is running:

```sh
octomate configure --url http://127.0.0.1:18080
```

This preserves an existing resolved credential; if none exists it generates one.
Register the user's secret in production through the existing `users.yaml` setup,
then run that harness's `hooks install` and `mcp install` commands. Check project
config, environment overrides and previously pinned URLs. MCP installations embed
their URL and credential. Pin compatible CLI/server versions for each rollout.

Tailcat device keys determine which computer can connect; Octomate bearers determine
which user a request represents. Retain both checks. Revoke a device by removing its
key and restarting the listener; verify established connections terminate too.
Rotate a user's bearer separately when removing the user, then restart Octomate to
reload its registry.

VPN or proxy software can affect outbound tunnel traffic. Test direct and relayed
paths, sleep/wake and reconnects with the intended network configuration.
Tailcat's wrapper remains experimental; keep versions pinned and test upgrades
before promotion. [Security model](https://github.com/tailscale/tailcat/blob/main/SECURITY.md).

### Optional background operation

For persistent remote access, supervise the Tailcat processes separately:

| Job | Location/context | Contract |
| --- | --- | --- |
| Tailcat server | Server process, unprivileged user | Saved server identity; approved client keys; serves only port 8000. |
| Tailcat forward | Client process | Bind 18080 to loopback; reconnect as verified. |

Maintain these jobs independently of Octomate. The Octomate CLI never creates,
starts, stops or upgrades them. An application upgrade leaves the tunnel running;
when Octomate restarts, clients reconnect through the existing forward.

### Connection checks

Repeat the working local hook, WebSocket and MCP checks through the client address
`http://127.0.0.1:18080`. Verify an unapproved device cannot connect, and an approved
device with an invalid Octomate bearer cannot use hooks or MCP.

Verify two concurrent clients, device revocation, key persistence, direct and relay
connections, and laptop sleep/wake. The forwarded port must be unreachable from
another client machine. Check coexistence with any configured VPN or proxy software.

## Verification status

The plan was checked against route registration, bearer verification, CLI command
registration and URL resolution, database settings, Alembic's environment, config
loading and startup behavior in the repository. CLI tests cover operation ordering
and failure handling with mocked service/Git/uv operations. Database tests use disposable SQLite files,
including the repository's actual migration chain. Local protocol verification is
tested with an in-process MCP application. Supervised startup, server installation,
production databases and cross-network Tailcat access have not been exercised.
