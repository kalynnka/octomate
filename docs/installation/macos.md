# macOS

The CLI prepares an isolated installation and manages it as a **GUI LaunchAgent**
owned by the desktop account that runs your coding agents. That choice is
deliberate: a driven Claude Code session reuses the desktop user's own Claude login
from the Keychain, with its connectors and plugins, and only a job in the logged-in
user's context has that. A system daemon does not, whatever `UserName` it sets.

## Prepare with the wizard

Install git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and your
harness. Log the harness in as your desktop user, without `sudo`. Then:

```sh
uv tool install octomate-cli
octomate service init --prepare
```

The wizard walks six steps: the installation directory (default
`~/Library/Application Support/Octomate`), the loopback port, which tentacles to
scaffold, their details, a review table, and the build. It picks the highest stable
`octomate-vX.Y.Z` release, clones it, syncs its locked dependencies, generates the
config home with three fresh auth salts in `.env`, validates the result in the
installed interpreter, and writes a draft LaunchAgent.

Non-interactively:

```sh
export OCTOMATE_INSTALL_ROOT="$HOME/Library/Application Support/Octomate"
octomate service init --prepare \
  --root "$OCTOMATE_INSTALL_ROOT" --port 8000 \
  --agent claude --agent codex --channel trunkline --yes
```

`--yes` needs `--root`, `--port`, `--agent` and `--channel` (`none` for no
channels), and skips the optional MCP preset, which only the interactive checkbox
offers. `--source /path/to/checkout` copies a local working tree instead of a
release, uncommitted files included and ignored files excluded; the result is a
snapshot with no release tag, so the managed upgrade will not apply to it.

!!! note "Preparation stops before anything runs"
    It creates no database, no account and no service. Slack, Lark and Discord
    blocks are written disabled with `FILL_IN_*` placeholders. `CONFIGURATION.md`
    in the installation lists what is left.

Re-running `octomate service init --prepare --root <root>` on a prepared root
validates the saved files and changes nothing. Pass no `--source`, `--port`,
`--agent` or `--channel` then; edit the files instead.

## Complete the installation

1. Work through `<root>/CONFIGURATION.md` and edit `config/tentacles.yaml`.
2. Review `control/io.octomate.server.plist`. It carries the environment the
   service will run with: `PATH` as captured from your shell, `HOME`,
   `OCTOMATE_HOME`, `OCTOMATE_DB_URL`, plus `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and any
   proxy or certificate variables that were set when you ran the wizard. The service
   sources no shell profile.
3. Follow [Server setup](server.md#set-the-service-context) from "Set the service
   context": initialize the database and build the console. Keep the generated
   salts.
4. Run the server in the foreground once, create your [account](accounts.md), and
   verify one driven reply before installing the service.

## Activate the GUI service

Stop the foreground server. Check that `~/Library/LaunchAgents/io.octomate.server.plist`
does not already belong to another installation, then install the reviewed draft:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp -n "$OCTOMATE_INSTALL_ROOT/control/io.octomate.server.plist" \
  "$HOME/Library/LaunchAgents/io.octomate.server.plist"
chmod 600 "$HOME/Library/LaunchAgents/io.octomate.server.plist"
octomate service start
octomate service status
octomate service verify
```

The CLI reads the plist from that fixed path and checks that it belongs to you, that
every path is absolute, that it runs `<checkout>/.venv/bin/octomate service serve`,
and that it is an `Aqua` session job. One managed service per desktop account.

**A desktop login is required.** Disconnecting SSH does not stop the service;
logging out does. After a reboot, sign in before expecting Octomate back. Prevent
sleep if the Mac must answer continuously. The guide enables neither automatic login
nor sleep changes.

## Daily commands

| Command | What it does |
|---|---|
| `octomate service status` | The job's enabled and loaded state, PID, revision and paths |
| `octomate service logs --follow` | Tail the service's stdout and stderr files |
| `octomate service verify` | Schema is current, `/octomate/mcp` and the console routes are protected. No model calls. |
| `octomate service start` | Enable and bootstrap the job. Refuses a stale schema. Already loaded, it verifies and changes nothing. |
| `octomate service restart` | Stop and start the same release. No migration. |
| `octomate service stop` | Stop, and keep disabled across logins until `start` |
| `octomate service upgrade` | Stop, back up, fetch the latest release, migrate, restart, verify |
| `octomate upgrade` | The standalone CLI only |

Run `service upgrade` from the standalone CLI, never from `app/.venv`; it refuses
otherwise. It also refuses tracked local changes and any release that does not
contain the installed commit. A failure after the stop leaves the service disabled
and names the backup; nothing rolls back on its own. Every operation appends a line
to `logs/server.log`, which `service logs` does not show. See
[Upgrades and backups](upgrading.md).

The console build is separate: rebuild Trunkline after a server upgrade.

Finish activation by sending a real request through a channel, approving an
expected action, and, if you configured one, exercising a connector. Repeat after a
restart.
