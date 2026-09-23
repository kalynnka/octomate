# Install on macOS

**This is Octomate's tested deployment flow.** The CLI prepares an installation,
then manages it as a GUI LaunchAgent under your desktop account. That keeps the
server in the same user context as your coding agents and their logins.

Prefer to have your agent do it? [Copy the setup brief](quickstart.md#agent-tldr).
The steps below take you through it yourself, starting with a local server, one
agent and Trunkline.

## 1. Prepare with the wizard

Install the [requirements](requirements.md). Run your chosen coding agent once
and complete its login as your desktop user, without `sudo`. Then install the
standalone operator CLI:

```sh
uv tool install octomate-cli
octomate --version
octomate service init --prepare
```

The wizard asks for an installation directory, a loopback port, and tentacles to
include. Keep port `8000` for this walkthrough. Select the agent you already use
and **Trunkline**. External channels can wait until the console works.

Preparation selects the highest stable `octomate-vX.Y.Z` release, clones it into
`app/`, installs its locked dependencies, and writes:

| File or directory | What it is for |
|---|---|
| `config/*.yaml` | Server settings and selected tentacles |
| `.env` | Three newly generated authentication salts |
| `CONFIGURATION.md` | A checklist tailored to your selections |
| `control/io.octomate.server.plist` | The draft GUI LaunchAgent |
| `logs/prepare.log` | Preparation output, including build errors |

It creates no database or account, builds no web console, and starts no service.
Selected Slack, Lark and Discord channels start disabled, with credential
placeholders to complete later.

??? details "Explicit choices and development checkouts"

    To prepare the default directory with Claude Code, Codex and Trunkline:

    ```sh
    export OCTOMATE_INSTALL_ROOT="$HOME/Library/Application Support/Octomate"
    octomate service init --prepare \
      --root "$OCTOMATE_INSTALL_ROOT" --port 8000 \
      --agent claude --agent codex --channel trunkline --yes
    ```

    `--yes` requires `--root`, `--port`, `--agent` and `--channel`. Use
    `--channel none` to omit channels. Optional MCP presets are offered only in
    the interactive wizard.

    `--source /path/to/checkout` copies a local Git working tree, including
    uncommitted files and excluding ignored files. Use this for development;
    it is not a clean release installation for managed upgrades.

## 2. Complete the installation { #complete-the-installation }

Set the root to the directory you chose. Run all remaining server commands from
that directory, and repeat these exports in each new setup terminal:

```sh
export OCTOMATE_INSTALL_ROOT="$HOME/Library/Application Support/Octomate"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
cd "$OCTOMATE_INSTALL_ROOT"
```

Work through `CONFIGURATION.md` in this order:

1. **Server address — `config/octomate.yaml`.** Keep `host: 127.0.0.1` and
   your chosen port. The examples below use `8000`.
2. **Browser sign-in — `config/auth.yaml`.** Set `auth.cookie_secure: false`
   for this loopback HTTP setup. The generated default is `true`, which requires
   HTTPS. Preserve the generated salt values, whether you keep them in `.env`,
   move them into YAML or supply them through the service environment.
3. **Agent — `config/tentacles.yaml`.** Keep your chosen agent enabled and use its
   normal approval mode: `default` for Claude Code or `user_review` for Codex.
   The harness supplies its own models and login.
4. **Console — the `trunkline` block in that same file.** Keep it enabled with
   your chosen agent in `agents`. You will add `static_dir` after the build.
5. **Optional integrations.** Leave unfinished channels disabled. Supply
   credentials in YAML or environment variables. If you choose `.env`, remove
   matching YAML placeholders, since YAML takes precedence.
   Follow each [channel guide](../usage/channels/index.md)
   when you are ready to connect it.

Driven runs reuse harness authentication, but Octomate disables inherited hooks,
plugins and MCP connections for those runs. Configure shared tools through
[Octomate's MCP proxy](../usage/mcp/proxy.md). Your own native sessions keep their
existing customisations.

Review the draft plist too. It captures `PATH`, `HOME`, the config and database
paths, and selected harness, proxy and certificate variables from the shell that
ran the wizard. A LaunchAgent does not source your shell profile.

Recheck the completed configuration:

```sh
octomate service init --prepare --root "$OCTOMATE_INSTALL_ROOT"
```

On an already prepared root this validates the saved files without changing them.
Omit the original selection flags. Also run the
[configuration and target check](configuration.md#validate-without-starting) and
confirm the URL names this installation's `octomate.db`. Validation does not test harness logins
or bot credentials.

## 3. Initialise the database

!!! warning "This command writes the selected database"
    Continue only after checking the resolved path. This walkthrough is for a new
    installation. For existing data, follow [Upgrades and backups](upgrading.md).

From the installation root:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" upgrade head
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/alembic" \
  -c "$OCTOMATE_INSTALL_ROOT/app/octomate/migrations/alembic.ini" current
```

## 4. Build the web console

```sh
cd "$OCTOMATE_INSTALL_ROOT/app/trunkline"
pnpm install --frozen-lockfile
pnpm build
cd "$OCTOMATE_INSTALL_ROOT"
```

In `config/tentacles.yaml`, add the build's absolute path to the existing
`trunkline` block. Replace `alice` and the agent id to match your setup:

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default
  trunkline:
    type: trunkline
    agents: [claude]
    static_dir: /Users/alice/Library/Application Support/Octomate/app/trunkline/dist
```

Merge the field into your existing file; keep any other tentacles you selected.
YAML does not expand `$OCTOMATE_INSTALL_ROOT`. The build directory must exist
before startup.

## 5. Run once and create your account

From the installation root:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service serve
```

Leave that terminal running. Open `http://127.0.0.1:8000`. In a second terminal,
repeat the exports from step 2, then create a registration invitation:

```sh
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service invite --url http://127.0.0.1:8000
```

This writes an invitation to the database and prints a private registration link.
Open it, register, then sign in. Reload the page to check that the login persists.
See [Accounts and tokens](accounts.md) for issuing your client token.

Start a new conversation in Trunkline with your chosen agent and ask a simple
question. A reply checks the harness login and the driven path. Then run the
[anonymous HTTP checks](server.md#run-and-verify); each enabled protected route
should return `401`.

## 6. Activate the GUI service { #activate-the-gui-service }

Stop the foreground server with **Ctrl+C**. Check that
`~/Library/LaunchAgents/io.octomate.server.plist` does not already belong to
another installation, then install the reviewed draft:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp -n "$OCTOMATE_INSTALL_ROOT/control/io.octomate.server.plist" \
  "$HOME/Library/LaunchAgents/io.octomate.server.plist"
chmod 600 "$HOME/Library/LaunchAgents/io.octomate.server.plist"
octomate service start
octomate service status
octomate service verify
```

`cp -n` does not replace an existing plist; resolve an existing installation before
continuing. The CLI manages one service per desktop account, from that fixed path.

**The desktop account must stay logged in.** Disconnecting SSH does not stop the
service; logging out does. After reboot, sign in before expecting Octomate to
return. A sleeping Mac cannot reliably serve requests; choose its sleep settings
to suit how you intend to use it.

## 7. Connect your native agent

Follow the [client quick start](clients/quickstart.md) on each machine where you
run a native agent. On this Mac, the standalone CLI is already installed.

You are done when a fresh native prompt and its answer appear in Trunkline, the
agent can call `gateway_scry` over MCP, and the console can still get a driven
reply. Run `octomate service restart`, then check that sign-in, history and a new
reply still work.

## Daily commands

For access from other devices, follow [Tailscale](networking.md#tailscale).
Keep the server private even with browser sign-in enabled.

| Command | What it does |
|---|---|
| `octomate service status` | Show the job's enabled and loaded state, PID, revision and paths |
| `octomate service logs --follow` | Tail service stdout and stderr |
| `octomate service verify` | Check the schema and protected MCP/console endpoints; makes no model calls |
| `octomate service start` | Enable and start the installed job; requires a current schema |
| `octomate service restart` | Stop and start the same release; performs no migration |
| `octomate service stop` | Stop and keep disabled across logins until `start` |
| `octomate service upgrade` | Upgrade the managed server, including database backup and migration |
| `octomate upgrade` | Upgrade the standalone CLI |

Read [Upgrades and backups](upgrading.md) before a server upgrade. Run it from the
standalone CLI, not `app/.venv`. The console build is separate and needs rebuilding
after the server checkout changes.
