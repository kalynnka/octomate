# macOS deployment wizard

Internal implementation design. This replaces the manual macOS bootstrap in
[server deployment](server-deployment.md); keep it out of public README links until
the CLI is stable. It does not authorize a minidock redeployment or production
database writes.

The CLI implements `upgrade`, GUI `service` management and the preparation wizard
`service init --prepare`. Preparation installs a checkout and its dependencies,
generates private configuration and independent authentication salts, validates
them in the installed interpreter, and saves a draft LaunchAgent under
`<root>/control/io.octomate.server.plist`. It does not install that definition into
launchd, create a database/account, or start a service. Daemon conversion,
activation and live verification remain separate implementation units.

For a local development demo, including uncommitted source changes:

```sh
uv run octomate service init --prepare --root /private/tmp/octomate-demo --source .
```

Choose an empty destination outside the source checkout. The wizard has separate
Installation, Network, Agents, Channels, Review and Prepare steps. Select Claude
Code, Codex and/or DSH (experimental; config key `deepseek`) under Agents;
select Trunkline, Slack, Lark and/or Discord
under Channels using arrow keys, Space and Enter. Channel selection may be empty.
Selections scaffold the corresponding agent and channel config templates. The
wizard never requests or imports channel credentials. Fill the `FILL_IN_*` values
in `config/channels.yaml` afterwards; Slack, Lark and Discord start disabled.
Each installation includes `CONFIGURATION.md`, a checklist for a person or agent
to complete the selected configuration and record live verification. Generated
authentication salts stay in `.env`. Trunkline’s frontend build is separate;
Napcat is not offered yet. The wizard uses neutral body text, dim hints, muted teal (`#5BA3B5`) for
headings and active choices, green for completion, amber for cautions and red for
failures. Color supports the labels rather than coloring every line.
It snapshots Git-visible files into an independent checkout and excludes ignored
local configuration/data. Without `--source`, it selects the latest stable release;
that release must contain the preparation maintenance action.

Repeat `service init --prepare --root <root>` to validate the saved configuration
without rewriting files or rotating secrets. Existing settings cannot yet be edited
through the wizard. `--yes` requires explicit `--root`, `--port`, `--agent` and
`--channel` choices for new installations (repeat for multiple channels; use
`--channel none` for no channels). No channel credentials are required, including
with `--yes`. Template validation checks configuration structure; it does not
establish readiness or authenticate with the selected services. A failed
preparation leaves its build log under `<root>/logs/prepare.log`; it does not
silently resume or overwrite a partially populated destination.

The remaining examples below describe the completed design, beyond the currently
available preparation workflow.

“脚手架” is **scaffolding**: generating the files and directories an application
needs. The interactive part is a **setup wizard**. Octomate needs both, behind one
entry point: `octomate service init`.

The primary minidock requirement is to reuse the desktop user's existing
Keychain-backed Claude.ai login, with their connectors and plugins available to
Octomate-driven Claude sessions. Moving to a GUI LaunchAgent serves that requirement;
starting a process successfully is not enough. Do not replace this login with
`claude setup-token`, copy its credential into `.env`, or create an isolated Claude
home. Claude owns credential access and refresh through its normal login mechanism.
[Claude credential storage](https://code.claude.com/docs/en/authentication).

## Operator experience

With uv installed, the first installation can be one command:

```sh
uvx --from octomate-cli octomate service init
```

For a persistent command on PATH, use:

```sh
uv tool install octomate-cli
octomate service init
```

These examples require a future CLI release containing the wizard. The bootstrap
CLI remains independent of the managed server's virtual environment, so a service
upgrade does not replace the interpreter running the deployment operation. No shell
profile editing or generated Python scripts are required.

Run on the target Mac as its desktop account, without sudo. Git and uv are the host
prerequisites; uv provisions the Python version declared by the selected release.
The wizard checks repository access and downloads before interrupting a service.
It explains missing prerequisites with an exact installation next step.

The default is one installation per desktop account, serving `127.0.0.1:8000` as a
GUI LaunchAgent. The wizard asks only for missing choices:

1. **Installation:** new installation or import the detected Octomate service;
   default directory `~/Library/Application Support/Octomate` for a new install.
2. **Configuration:** create configuration or use an explicitly selected config
   directory and secrets file. Choose a new database or explicitly adopt an existing
   one; display its resolved absolute path. Never discover a development database
   and silently make it production.
3. **Runtime:** choose Claude Code for the initial supported wizard preset. Use
   the desktop account's existing Keychain-backed Claude.ai login, with a guided
   login step only if needed. Retain its Claude settings, enabled plugins and
   marketplaces. Existing configurations retain their configured agents.
4. **Account and channels:** create the first Octomate account on a new database,
   or select an existing account. Optionally configure a supported IM channel;
   skipping channels leaves a usable authenticated API. Scaffold only the selected
   channel templates and generate their completion checklist; the operator fills
   credentials later. Projects are optional and explicitly selected by the operator.
5. **Verification:** select an available Claude.ai connector and a specific safe
   read operation, such as reading an operator-selected test document. Show the
   operation and explain that verification makes model requests and records test
   conversations. A deployment without connector requirements may omit this check;
   minidock must include it. Select required plugins and a safe capability to
   exercise, including the project/workspace where project-scoped plugins apply.
6. **Review and deploy:** show paths, version, launch account/domain, enabled
   integrations, file changes, database initialization/migrations, account changes,
   and verification actions. One confirmation covers that concrete operation.

Channel credentials are filled in the private config after scaffolding. Browser
login and provider consent remain human steps; “one command” does not promise silent credential provisioning.
Missing login or permissions leave a precise next step, not a successful result.

### CLI presentation

Use Typer with the existing Rich dependency to make deployment and service commands
clear and pleasant to use. Follow the CLI's existing `Console` and `Panel` usage;
choose the appropriate Rich components during implementation without adding a UI
framework or a general presentation abstraction.

- Group wizard prompts into short, numbered steps with visible defaults and
  multi-select agent/channel menus. Use a compact table or panel for the final deployment review.
- Show the current phase and elapsed time for downloads, dependency installation,
  migrations and verification. Use a progress bar when the total is known and a
  spinner otherwise; do not fabricate percentages or hide subprocess failures.
- Present status and before/after versions in aligned tables. Label results with
  words such as “Passed”, “Failed” and “Not checked”, with restrained color as a
  supplement. End operations with a concise result and relevant next commands.
- Errors name the failed phase, actual service state, log/backup paths and next
  action. Keep secrets redacted and render external values as literal text rather
  than Rich markup. Keep commands and paths easy to copy.
- Respect terminal width and `NO_COLOR`. When output is redirected, use plain,
  stable lines without ANSI escapes, spinners or cursor movement. Keep progress
  and diagnostics on stderr so stdout and followed logs remain usable in pipes.

## Command surface

| Command | Contract |
| --- | --- |
| `octomate service init` | First-run wizard; subsequently apply the saved deployment at the installed release, start if needed, and verify. No implicit release upgrade. |
| `octomate service init --configure` | Revisit settings, validate and show the changes, then apply and restart if necessary. Preserve omitted values. |
| `octomate service init --prepare` | Install the checkout/dependencies and scaffold or validate config; do not start/stop services, initialize/migrate databases, create accounts, or run live requests. |
| `octomate service init --yes` | Apply complete saved inputs without the final prompt. Fail on missing input; never invent credentials or bypass provider approval. |
| `octomate upgrade` | Update the separately installed operator CLI through its package installer. Does not change the deployed service. |
| `octomate service upgrade` | Upgrade the deployed application to the latest stable server release, with locked dependencies, backup, rehearsal, migration, restart and verification. Does not update the separately installed operator CLI. |
| `octomate service invite` / `octomate service user …` | Manage invitations and accounts in the service database. |
| `octomate service status` | Read-only domain, enabled/loaded state, PID, installed release, config/database paths and last verification result with timestamp. |
| `octomate service logs --follow` | Read the configured service logs. |
| `octomate service start\|stop\|restart` | Manage the installed release without changing configuration or migrating. Start refuses a schema mismatch and directs the operator to deploy. |
| `octomate service verify` | Check service identity, configuration, schema and protected routes; no paid requests or test records. |
| `octomate service verify --live` | Additionally run the saved Claude, connector and plugin checks through the managed server, with the operations shown before execution. |

On an existing installation, `--prepare` writes proposed changes to a separate
candidate location; it never changes the files read by the running server.
For a custom root prepared before a plist exists, pass the same `--root` when
applying it. Account passwords are not saved for a later `--yes`; first-account
creation still needs hidden input or an explicitly supplied protected input file.

Deployment and service upgrade include live verification when the saved requirements
call for it. They make the same check available separately through `service verify`.
Ordinary restart performs basic verification without issuing paid requests.
An unchanged, already running `init` verifies without restarting or migrating.

`--root <path>` selects a new installation directory. Once installed, commands find
the checkout and environment from the standard user plist, independent of the
current directory. The CLI generates and owns that plist as a macOS implementation
detail. There are no plist-path arguments or compatibility commands. When both
standard GUI and system definitions exist without an import record, the deploy
wizard requires an explicit choice before conversion. Never manage both jobs
because they share a label.

Launchd and Docker run the foreground server through `octomate service serve`, the
CLI command for foreground execution. It also supports foreground/tmux
development. `service start` manages the installed GUI LaunchAgent. Use `configure`
for client settings.
CLI operations belong at the root; all server operations belong under `service`,
including `service init`, `service invite` and `service user`. Remove
the old plist options and `cli` group; do not retain aliases. Routine
service commands manage only the installed GUI service. An existing system daemon
must go through the setup wizard's one-time conversion first.

### Independent upgrades

The operator chooses which installation to update:

```sh
octomate upgrade
octomate service upgrade
```

`upgrade` targets the standalone `octomate-cli` distribution and its compatible
dependencies. For the recommended `uv tool install` installation, delegate to
`uv tool upgrade octomate-cli`, then report the version from the updated executable.
Verify installer ownership first. If invoked from the service virtual environment,
refuse to update that environment and direct the operator to their standalone CLI.
For another installer, identify the installation and report its appropriate update
command; do not guess an interpreter, switch installers or fall back to sudo.
An invocation through `uvx` is temporary, not an installed CLI to update; report how
to refresh that invocation or install the persistent tool.

`service upgrade` selects stable server releases using the existing server tag
filter and updates only the managed checkout and its locked environment. That
environment includes the server's declared CLI dependency; it is separate from
the operator's installed CLI. Report both versions distinctly. Check required
CLI/service capabilities before mutation and name the needed upgrade if incompatible;
neither command implicitly runs the other.

## Files and configuration

Each service owns its configuration and data directly under its installation root,
with all paths made absolute:

```text
<root>/app/                       release checkout, uv.lock, .venv/
<root>/config/                    existing per-subsystem server YAML format
<root>/.env                       server secrets
<root>/octomate.db                new-install SQLite default
<root>/.octomate/                 runtime-owned mirrors, workspaces and other state
<root>/control/                   operation lock, import record, verification recipe
<root>/backups/                   SQLite snapshots and changed configuration backups
<root>/logs/                      service output and deployment results
~/Library/LaunchAgents/io.octomate.server.plist
```

Run the service and maintenance with `<root>` as their working directory, so the
existing `.env` and `.octomate/` resolution stays local to this installation. Set
`OCTOMATE_HOME=<root>/config` and the explicit database URL to `<root>/octomate.db`.
Git and uv operations target `<root>/app` explicitly. No shared directory or
configuration symlinks are created, including in project mirrors/workspaces.

Update maintenance's current `cwd.parent` assumptions for backups, control and
logs to use the installation root, and resolve its interpreter from `app/.venv`.
An imported installation keeps its existing paths and log destinations during
launch-context conversion; moving existing production data is a separate operation.
Do not overwrite unrelated files or symlinks at a chosen destination.

The plist is the service definition; YAML and `.env` are application configuration.
Do not introduce a second deployment configuration language duplicating their
values. A small typed verification recipe holds only the selected user ID,
connector/tool identity, required plugins, project context, validated test arguments
and expected result criteria.
The import record retains the old definition, domain and prior enabled/loaded
state for manual rollback. The verification recipe contains no login credentials.
An archived plist can contain existing environment secrets; protect it as a secret
file and never print its unredacted contents.

Generate config using the selected release's models and packaged examples:

- Explicit loopback bind, selected port, Claude agent, and empty channel/project/MCP
  mappings unless selected. Never enable an integration because an example has it.
- Reuse existing `OctomateConfig` and channel models for template validation.
  Pass only selected names to the generator; no preparation models belong in the
  shared protocol. Credential-requiring channel templates remain disabled until
  the operator completes their fields and enables them.
- New installations generate independent auth salts and, when needed, an OAuth
  encryption key. Existing installations preserve all such material exactly;
  missing encryption material is an error, not a reason to replace it.
- Generate authentication salts in `.env`, omitting their YAML fields. Channel
  templates have explicit `FILL_IN_*` placeholders in private `channels.yaml`. The
  operator replaces these, or removes those YAML fields when moving their values
  into `.env`: environment overrides YAML, which overrides `.env`. The generated
  checklist explains this and lists only the selected agents and channels.
- Validate with a fresh process in the service working directory and its exact
  environment. Do not inherit arbitrary `OCTOMATE_*` variables from the caller.
- New private directories use mode `0700`; secret/config/backup files use `0600`.
  Write changed files atomically after validation, retaining their prior contents.
  Do not rewrite unchanged config or rotate credentials on rerun.

The wizard uses `AuthManager` and Arcanus for first-account registration and token
issuance after the database is ready. Reuse invitation, registration and API key
policies; do not add SQL seed scripts, a default password or a second identity model.
Configure the local CLI through the existing config writer, with a preview if it
would replace an existing client target. Do not automatically overwrite native
Claude/Codex hooks or MCP settings; installation of those remains an explicit option.

## GUI service contract

A GUI LaunchAgent is the macOS default because it runs in the logged-in user's
context. A system daemon's `UserName` does not give it that context. This supports
the proposed minidock fix; it does not by itself prove authentication succeeds.
[Apple's process context documentation](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/DesigningDaemons.html).

Derive UID and home from the current account, reject root, and check that
`launchctl print gui/<uid>` succeeds before changing an existing service. An SSH
session can invoke the CLI while that desktop session exists; SSH login alone is
insufficient. Routine GUI operations use unprivileged `launchctl` with explicit
`gui/<uid>` targets, never an implicit caller domain or `sudo -u` substitute.

Generate the following plist values rather than asking the operator to write XML:

- `Label: io.octomate.server`, `LimitLoadToSessionType: Aqua`; no `UserName` or
  `GroupName` for the GUI job.
- Absolute `ProgramArguments: [<app>/.venv/bin/octomate, service, serve]` and
  `WorkingDirectory: <root>` for new installations; no shell wrapper or shell
  activation. Imported services retain their existing working directory.
- `RunAtLoad: true`, `KeepAlive: true`, `ThrottleInterval: 10`,
  `AbandonProcessGroup: false`, and `Umask: 63` (decimal for octal `077`).
- Absolute stdout/stderr paths and explicit `HOME`, `USER`, `LOGNAME`, `PATH`,
  `OCTOMATE_HOME` and `OCTOMATE_DB_URL`. Preserve required proxy, certificate,
  provider and Claude configuration-directory variables from an imported service.

Resolve required executable locations during setup; do not hard-code Homebrew's
architecture-specific prefix. Verify the Claude executable actually used by the
installed Agent SDK, not merely the first `claude` found by the interactive shell.

Claude.ai connectors require an active subscription login. The wizard must detect
conflicting API-key, provider/profile, `apiKeyHelper`, connector-disable settings
and exported-token overrides, and show the required correction. In particular,
Anthropic documents that `claude setup-token` tokens only support model requests.
Do not extract Keychain secrets or silently switch authentication methods.
[Claude.ai connector requirements](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claudeai).

Plugin availability also depends on settings and workspace, separately from login.
Preserve the desktop user's `HOME`, existing `CLAUDE_CONFIG_DIR` if set, Claude user
settings, plugin installations/cache and marketplace configuration. Do not replace
them with a deployment-specific Claude home or reinstall/update plugins during
deployment. Inspect the effective configuration with secrets redacted.

Octomate currently leaves SDK `setting_sources` unset, and its tests assert that
choice. The current SDK reference describes this as loading the CLI's default
sources. Preserve that behavior and verify it against the installed SDK/Claude
versions; do not set an empty source list or restrict loading to project settings.
[Python SDK settings sources](https://code.claude.com/docs/en/agent-sdk/python).

User-scoped plugins and project/local plugins have different activation contexts.
Octomate runs Claude in a thread workspace, not necessarily the service checkout
or the directory used in a successful desktop test. Verify required project/local
settings and plugin paths in that actual workspace. If a local setting or plugin
path is absent there, show the specific mismatch and proposed correction; do not
copy all desktop settings into every workspace or globally enable a project plugin.
[Plugin scopes and installation paths](https://code.claude.com/docs/en/plugins-reference).

After reboot the desktop account must log in; logout stops the agent. Screen lock
is not logout, but sleep can interrupt availability. Do not enable automatic login
or change sleep settings as part of deployment. Show this lifecycle in the wizard
and status output. Pre-login availability needs a different deployment design.

## Apply, service upgrade and failures

Reuse the existing release selector, operation lock, maintenance subprocess,
SQLite backup and migration rehearsal. The CLI owns orchestration; maintenance
runs inside the managed release so it loads the matching models and migrations.

1. Validate paths, GUI session, service ownership, effective configuration and
   database identity. Resolve/download a release for new installation or upgrade.
   Refuse a dirty or divergent managed checkout before stopping a running service.
2. Show the concrete changes and obtain the one operation confirmation, or use
   explicit complete inputs with `--yes`. Include database and account writes in
   that confirmation. `--prepare` stops before these writes.
3. Lock the deployment. Recheck the reviewed inputs; abort if they changed. Disable
   and boot out the exact managed job when changes require stopping it. Confirm its
   PID/process group and listener are gone; a free port alone does not prove its
   Claude subprocesses stopped. Refuse to kill unrelated port owners.
4. Back up existing SQLite state using the backup API, plus changed config and
   encryption material. Upgrade code/dependencies when requested. Rehearse pending
   migrations on a distinct verified copy, then migrate the explicit production
   target. A new database is initialized only after the operator chose that path.
5. Apply configuration, complete requested first-account setup, validate the plist,
   and enable/bootstrap `gui/<uid>`. Failures stop here with the service disabled.
6. Verify the expected service identity and protected API, then the configured live
   connector/plugin requirements. First installation and daemon import also restart
   once and repeat the live checks against the new PID. Upgrade verifies its newly
   started process.
7. Record version, PID, schema revision, backup locations and individual check
   outcomes. Report success only when every required check passes.

`stop` means disable plus bootout so KeepAlive and a later login do not undo the
operator's intent; `start` enables and bootstraps. `restart` stops and starts the
same release with bounded waits. Each mutating command uses the same lock.

Before service mutation, a failed check leaves the old service running. After
mutation, a failed apply leaves it disabled and prints the failed phase, actual
state, logs and next command. Do not automatically retry a migration, restore a
database or downgrade code. A retry inspects current files/schema/job state and
fails on inconsistent inputs; no durable deployment workflow engine is needed.
Inspection-only `verify` reports failure without stopping a running service.

Keep existing log destinations on import. New installs capture stdout/stderr and
deployment results; launchd redirection itself does not rotate logs. Preserve any
existing rotation, document the initial size monitoring/manual retention procedure,
and leave a new rotation subsystem to a separate change.

## Verification through Octomate

HTTP reachability and a direct terminal `claude` request are insufficient. The live
test must be dispatched by the already running GUI-managed Octomate process:

1. Verify the loaded job's binary, account, working directory and PID match the
   intended installation, and that the responder identifies that process/release.
2. Verify unauthenticated MCP rejection, authenticated MCP initialization/discovery,
   and the configured console-route state. Trunkline remains disabled by default;
   do not build a frontend just to deploy an API server.
3. Submit a bounded Claude request that returns a nonce supplied for this run.
4. Submit the saved connector read request. Require an actual tool-call event and
   matching successful tool result from the selected Claude.ai connector, followed
   by run completion. Tool listing, a model's claim of success, and an unrelated
   Octomate MCP tool do not satisfy this check.
5. Compare required enabled plugins and their scope/version with the effective
   runtime inventory. Exercise a selected plugin's safe tool or skill in its intended
   workspace and require observable execution evidence, not just an installed file
   or the model saying it is available. Mark other plugin capabilities as inventoried
   but untested; this does not certify every hook or tool supplied by every plugin.
6. After the first-install/import restart, require a different PID and repeat the
   model, connector and plugin checks. Record run IDs, tool/plugin identity,
   timestamps and pass/fail, without copying connector content into deployment logs.
   Preserve normal conversation records.

There is currently no general CLI request command suitable for this API-only flow.
Add a narrow project-owned verification router bound to the existing Octomate
instance, plus its CLI client. It must call Octomate's normal dispatch and deferred
approval handling; it must not launch a second SDK client from the deploy process.
This is an explicit implementation requirement, not a capability of today's
`octomate_cli.deployment.verify`.

Use an authenticated, short-lived API key with a dedicated verification scope and
the selected user's identity, issued through `AuthManager`. Keep the token in memory,
send it in headers, revoke it on completion, and enforce expiry if the CLI exits.
The router accepts only the saved verification recipe and a nonce, restricted to
the configured operator user; it exposes no lifecycle, shell or migration actions.
Return structured events/results including process identity. Provider approvals
must reach the operator through the CLI; never change the deployment's permission
mode to make a smoke test pass. A timed-out test must cancel its run and fail.

Connector selection is provider-specific: the wizard accepts an exact tool and
arguments or a reviewed preset, validates them against discovery, and does not
infer that an arbitrary tool is safe from its name. If a connector requires
interactive permission on each run, verification reports that requirement.

## minidock conversion

After installing the CLI release with this support, log into minidock's desktop
as its service account and run:

```sh
octomate service init
```

The wizard detects `/Library/LaunchDaemons/io.octomate.server.plist` and offers
conversion to the desktop account's GUI service. The operator selects that option;
no plist argument or legacy service-management command is exposed.

The command imports the executable, checkout, database, config, logs and explicit
environment. It does not upgrade the application, sync dependencies, move data or
migrate the schema. Refuse a schema mismatch; handle that as a separate upgrade.
If the installed server lacks the verification endpoint, report the required
compatible release before touching launchd; install that release as a separate,
reviewed upgrade before conversion.

Prepare and validate the new GUI plist and rollback record before downtime. The
single review shows the old `system/<label>` and new `gui/<uid>/<label>` targets.
Only the old system job's disable/bootout requires sudo; obtain that privilege before
stopping anything. Retain its root-owned definition at the original path and record
its contents and prior enabled/loaded state. Never load both jobs concurrently.

Once the daemon and its children have stopped, install and bootstrap the GUI plist,
run the Keychain-login, connector and plugin checks, restart, and repeat. Record the
old system job as disabled and absent, with exactly one GUI server process serving
the database.
Live verification creates ordinary test records, but conversion changes no schema.

If conversion fails, leave both definitions available and the failed GUI job
disabled. Print exact manual commands to boot out the GUI job and restore the
system job's recorded prior state. This restores the old launch context, including
its known Claude problem; it is not proof of functional recovery. Because conversion
does not change code/schema, it requires no database rollback. General upgrade
rollback still requires an explicit compatibility decision and may lose later work
if an operator restores a snapshot.

## Implementation and acceptance

Keep three separately reviewable units, presented in dependency order:

1. **Service model and lifecycle:** GUI service ownership, correct account
   environment, installation root distinct from checkout, generated plist and
   unprivileged `service` commands. Reuse the existing launchctl logic in
   `cli/octomate_cli/service.py`, removing its plist-based command interface. Keep
   system-domain handling confined to the one-time conversion; do not add a
   cross-platform service framework.
2. **Configuration and wizard:** config/verification input models first, then
   existing maintenance/auth owners, then `init` and CLI registration. Reuse
   config defaults and release selection. Keep server imports out of client-only
   CLI startup. Do not expose “deployment complete” until live verification exists.
   Register root `upgrade` for the CLI and `service upgrade` for the deployed
   application; keep their installation targets separate.
3. **Live verification:** typed request/result and auth scope, Octomate-owned
   dispatch/router integration, then CLI orchestration and minidock acceptance.
   Update the operational guide when these commands actually ship.

Automated tests use mocked launchctl and test-created databases/directories:

- GUI targets never use sudo; imported system targets do. Missing desktop session,
  wrong user, conflicting job, occupied port and invalid plist stop before mutation.
- CLI help exposes root `upgrade` and `service upgrade`, with all server commands
  under `service` and no `cli` group, plist-path options or `server` group.
- CLI upgrade leaves the service checkout, environment, job and database untouched;
  service upgrade leaves the standalone CLI installation untouched. Refuse a CLI
  self-update invoked from the managed service environment.
- Presentation checks cover narrow terminals, redirected output, `NO_COLOR`, secret
  redaction and literal rendering of paths/errors containing Rich markup characters.
- Config, secrets, database and runtime state resolve within the service's own
  root; no shared directory or config links into mirrors/workspaces are generated.
- First-run defaults validate; secrets resolve from `.env`; repeat setup preserves
  keys/accounts/files; invalid or partial configuration cannot overwrite good config.
- Preparation never writes a database or starts a service. Unchanged setup never
  restarts/migrates. Upgrade retains copy rehearsal and resolved-target checks.
- Import preserves paths/environment/release and retains the original definition;
  each failure leaves the documented enabled/loaded state and rollback information.
- Verification denies missing/wrong-scope/wrong-user credentials, uses the managed
  server, observes a real selected tool result, handles approvals/timeouts, and
  requires a new PID after restart.
- Claude home/settings sources are preserved; required user and project plugins
  load in their actual thread workspaces. Neither a setup token nor a fresh Claude
  configuration directory is introduced to bypass an authentication failure.

Manual acceptance on a disposable Mac deployment covers new installation with no
prewritten files, upgrade, stop/start, process failure, SSH disconnection, and
reboot followed by desktop login. Verify logout stops service and login starts it.
Repeat real Claude, connector and plugin checks on minidock after an explicitly
authorized conversion. Report numeric automated results separately from these live
checks.

Linux/Windows supervisors, remote SSH orchestration, unattended desktop login,
automatic updates/rollback, general recovery infrastructure, frontend builds and
Tailcat provisioning are outside this first delivery.
