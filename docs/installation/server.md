# Manual setup

Use this to install from a source checkout and run the server in the foreground.
Reuse the configuration generator or start from the packaged YAML templates,
then review each setting. The commands below assume a Unix shell.

!!! info "Deployment support"
    **macOS is the only deployment flow tested end to end.** Its
    [walkthrough](macos.md) also prepares a LaunchAgent. The shared wizard can
    prepare a [Linux user service](linux.md) or [Docker installation](docker.md).
    This page covers foreground operation and configuration by hand.

## Keep the installation separate

One directory, owned by the account that runs the server and separate from any
checkout you develop in:

```text
<installation>/
  app/                     the release checkout and its .venv
  config/                  the server's YAML files, the config home
  .env                     optional local source of settings and secrets
  octomate.db              the database
  .octomate/               mirrors, workspaces, downloaded files
  control/                 service-control files, where applicable
  backups/                 database snapshots
  logs/                    service output, where applicable
```

Run the server and maintenance commands with the installation as the working
directory. That is where the server stores `.octomate/` and reads `.env`, if used.
`OCTOMATE_HOME` selects configuration; it does not move the working data.

The guides use `OCTOMATE_INSTALL_ROOT` as a shell variable for this directory.
Octomate does not read that variable itself.

## 1. Create the installation { #create-the-installation }

Install the [requirements](requirements.md) and log your chosen harness in as the
account that will run the server. Keep the operator CLI available separately:

```sh
uv tool install octomate-cli
octomate --version
```

Choose a new installation directory. Replace `octomate-vX.Y.Z` below with an
actual **server** tag from [Releases](https://github.com/kalynnka/octomate/releases);
CLI and protocol tags name different packages.

```sh
export OCTOMATE_INSTALL_ROOT="$HOME/.local/share/octomate-server"
mkdir -p "$HOME/.local/share"
mkdir -m 700 "$OCTOMATE_INSTALL_ROOT"
cd "$OCTOMATE_INSTALL_ROOT"
mkdir config control backups logs
git clone --branch 'octomate-vX.Y.Z' --depth 1 https://github.com/kalynnka/octomate.git app
uv sync --locked --no-default-groups --project "$OCTOMATE_INSTALL_ROOT/app" --python 3.13
```

The installation directory creation should succeed without replacing an existing
installation. Keep the whole checkout, including its `cli/` and `protocol/`
workspace packages. `--no-default-groups` installs the server's runtime
dependencies without the development or documentation groups.

## 2. Set the service context { #set-the-service-context }

In every shell used for setup:

```sh
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
cd "$OCTOMATE_INSTALL_ROOT"
```

The absolute Unix path produces four slashes after `sqlite+aiosqlite:`. The
database variable is `OCTOMATE_DB_URL`, with one underscore before `DB`.

## 3. Prepare the YAML configuration

### Use the configuration generator

The interactive wizard supports macOS and Linux, with `--target manual` for
foreground operation. If you already created the checkout above, invoke the same
configuration generator directly from the installed server environment:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/python" -m octomate_cli.deployment prepare \
  --target manual --port 8000 --agent claude --channel trunkline
```

Use `--agent codex` instead if that is your harness; repeat `--agent` for several.
You can also repeat `--channel` with `slack`, `lark` or `discord` to generate
disabled channel templates. DeepSeek Harness is available as `--agent deepseek`
and remains experimental.

This is an **internal entry point used by the wizard**, not a cross-platform
service installer. Run the copy from the checked-out release. It writes seven YAML
files, fresh auth salts in a private `.env`, and `CONFIGURATION.md`, then
validates their structure. It neither creates the database nor installs or starts
a service. It refuses an existing `.env`, checklist or nonempty config directory.

The generator chooses `.env` for salts, but Octomate also accepts them in YAML or
the service environment. Preserve their values if you move them. The generated
checklist follows the selected deployment target. Continue with the steps below.

### Or start from the templates

The checkout includes seven commented templates in
[`octomate/config/defaults/`](https://github.com/kalynnka/octomate/tree/main/octomate/config/defaults).
For a new, empty config home, copy them instead of running the generator:

```sh
cp -n "$OCTOMATE_INSTALL_ROOT/app/octomate/config/defaults/"*.yaml "$OCTOMATE_HOME/"
```

Uncomment the blocks you need and fill in their values. The templates are a
reference: copying them alone enables no agent or channel. Configure sign-in
with three independently generated salts as described in
[Server settings](settings.md#browser-sign-in), then review the files below.

## 4. Review the configuration, one file at a time

### Server address — `config/octomate.yaml`

Keep the initial service on loopback:

```yaml
host: 127.0.0.1
port: 8000
```

Leave the generated mirror and workspace settings at their defaults. The explicit
`OCTOMATE_DB_URL` from step 2 supplies the database location.

### Sign-in — `config/auth.yaml`

The generator creates the three authentication salts for you; when using the
templates, supply them in YAML or environment variables. Preserve these values.
For the initial HTTP setup, set:

```yaml
auth:
  cookie_secure: false
```

This is an excerpt: edit that field in the existing file. Set it to `true` when
the browser reaches Octomate through HTTPS. See [Server settings](settings.md) for
how salts and OAuth encryption keys differ.

### Agent and console — `config/tentacles.yaml`

Keep one agent and Trunkline enabled. For Claude Code:

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default
  trunkline:
    type: trunkline
    agents: [claude]
```

For Codex, use an id such as `codex`, `type: codex`,
`permission_mode: user_review`, and `agents: [codex]`. Edit the generated blocks;
keep other selected channels disabled until their credentials are ready.

The harness uses the service account's login. A login on a different machine is
not automatically available here. [The tentacle registry](tentacles.md) explains
agent selection, channel bindings and adding an MCP preset with the CLI.

### Optional files

| File | When to configure it |
|---|---|
| `projects.yaml` | When you want driven agents to work on a [registered project](projects.md) |
| `providers.yaml` | When using [Inkling](../usage/agents/inkling.md) with model providers |
| `oauth.yaml` | When linking chat profiles or authorising [MCP connections](../tentacles/mcp.md) |
| `observability.yaml` | When changing logging or opting into [tracing](observability.md) |

Leave these defaults alone for the first console conversation. Supply credentials
in YAML or environment variables; `.env` is an optional convenience.
[Configuration](configuration.md#precedence-and-secrets) explains the precedence.

## 5. Validate, then initialise the database { #initialize-the-database }

Run the [read-only configuration check](configuration.md#validate-without-starting).
Confirm the explicit database URL names `<installation>/octomate.db`.

!!! warning "The next command writes that database"
    This walkthrough assumes a new installation. For existing data, stop the
    server and follow [Upgrades and backups](upgrading.md) before migrating.

From the installation root:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" upgrade head
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" current
```

Starting the server never applies migrations. Check for migrations when upgrading.

## 6. Build Trunkline { #build-trunkline }

```sh
cd "$OCTOMATE_INSTALL_ROOT/app/trunkline"
pnpm install --frozen-lockfile
pnpm build
cd "$OCTOMATE_INSTALL_ROOT"
```

Add `static_dir` to the existing `trunkline` block in `config/tentacles.yaml`.
Replace this example with the absolute build path on your host:

```yaml
  trunkline:
    type: trunkline
    agents: [claude]
    static_dir: /home/alice/.local/share/octomate-server/app/trunkline/dist
```

YAML does not expand shell variables. The directory must exist before startup.
Without `static_dir`, Trunkline's API is available but the web page is not served.

## 7. Run and verify { #run-and-verify }

From the installation root:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service serve
```

Open `http://127.0.0.1:8000`. Leave the server running and use a second terminal
for the checks below. Substitute your port, and use `/hooks/codex` or
`/hooks/deepseek` if that is the enabled runtime:

```sh
curl -i http://127.0.0.1:8000/api/trunkline/health
curl -i -X POST http://127.0.0.1:8000/octomate/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -i -X POST http://127.0.0.1:8000/hooks/claude \
  -H 'Content-Type: application/json' -d '{}'
```

Each enabled protected route should answer **401** without credentials.
A **503** from the console API means account authentication is unconfigured.
A missing hook route indicates its agent tentacle is not enabled; check startup
logs as well as the HTTP status. A listening port alone does not verify an agent
login or channel connection.

[Create your account and issue a token](accounts.md), then send a simple request
to your agent in Trunkline. Once you get a driven reply, connect your
[native client](clients/quickstart.md).

Once the foreground setup works, arrange service supervision for your host using
the same account, working directory and environment. Stop the foreground process
before starting a service on the same port. For access from other devices, use
[Tailscale or another private connection](networking.md#tailscale).
