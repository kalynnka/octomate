# Linux

Use the same configuration wizard as macOS, then run Octomate as a **systemd user
service** under the account that owns the agent logins. A desktop session is not
required.

!!! info "Deployment support"
    macOS remains the only deployment tested end to end with live agents.
    Linux preparation is covered by automated tests; verify your chosen agents
    and channels on your host before relying on unattended operation.

## 1. Prepare the installation

Install the [requirements](requirements.md), including Node.js and pnpm if you
want Trunkline. Log your agents in as the service account, without `sudo`.

```sh
uv tool install octomate-cli
octomate service init --prepare --target systemd
```

On Linux, `systemd` is the default target. The wizard asks for a new installation
directory, port and tentacles, installs the selected release, generates the seven
YAML files and validates their structure. It writes a draft
`control/octomate.service`; it does not install the unit, start the server or
create a database.

To try changes before they ship in a release, run the CLI from your development
checkout and pass that checkout as `--source`:

```sh
uv run --no-sync octomate service init --prepare --target systemd --source "$PWD"
```

The destination must be outside the checkout. On a distribution without systemd,
choose `--target manual` and follow [Manual setup](server.md) for foreground use.
For Windows, use the [Docker Compose guide](docker.md#windows); native deployment
is not available yet.

## 2. Review the configuration

In a new shell, set the installation path you chose:

```sh
export OCTOMATE_INSTALL_ROOT="$HOME/.local/share/octomate-server"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
cd "$OCTOMATE_INSTALL_ROOT"
```

Complete `CONFIGURATION.md`. Review the [server settings](settings.md) and
[tentacle registry](tentacles.md). The commented
[YAML templates](server.md#or-start-from-the-templates) explain every section.
Keep the initial bind address at `127.0.0.1`. Set `auth.cookie_secure: false` for
local HTTP, and restore `true` when using HTTPS.

Secrets may be supplied in YAML or the service environment. The wizard puts
generated salts in a private `.env` as a convenience; preserve their values if
you move them. The user service does not inherit your interactive shell's
exports. Review its `Environment=` entries when providing settings there.

```sh
octomate service init --prepare --target systemd --root "$OCTOMATE_INSTALL_ROOT"
```

This rechecks configuration without changing the files or the database. It does
not verify agent credentials.

## 3. Complete the first foreground run

Continue with the shared steps, in order:

1. [Validate and initialise the new database](server.md#initialize-the-database).
   This explicitly writes the database at the URL above.
2. [Build Trunkline](server.md#build-trunkline) and set its `static_dir`.
3. [Run and verify](server.md#run-and-verify), then
   [create your account](accounts.md#register-the-first-account).
4. Send a request through Trunkline and confirm a reply from your selected agent.

Keep agent credentials under the same account and home directory the unit uses.
For headless Codex login, use its device-code flow; see the
[authentication guide](https://developers.openai.com/codex/auth/).

## 4. Install the user service

Stop the foreground process with **Ctrl+C**. Review
`control/octomate.service`, especially its working directory, executable and
environment. If `~/.config/systemd/user/octomate.service` already exists, resolve
that installation before continuing.

```sh
mkdir -p "$HOME/.config/systemd/user"
cp -n "$OCTOMATE_INSTALL_ROOT/control/octomate.service" "$HOME/.config/systemd/user/octomate.service"
systemctl --user daemon-reload
systemctl --user enable --now octomate.service
systemctl --user status octomate.service
```

The unit runs with your UID, uses a private file-creation mask, restarts on
failure, and sends logs to the journal. To let the user manager start at boot and
remain after logout:

```sh
loginctl enable-linger "$USER"
```

Your distribution may require administrator authorization for lingering. See
[systemd's loginctl documentation](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html).
Agent credentials that depend on an unlocked desktop keyring still need separate
attention; lingering does not unlock a keyring.

## 5. Verify and connect

Repeat the console request after a restart, check history, then follow the
[client quick start](clients/quickstart.md) on your native agent machines.
Use [Tailscale](networking.md#tailscale) for access from other devices; public
internet exposure is not recommended even with Octomate login enabled.

| Task | Command |
|---|---|
| Status | `systemctl --user status octomate.service` |
| Follow logs | `journalctl --user -u octomate.service -f` |
| Restart | `systemctl --user restart octomate.service` |
| Stop | `systemctl --user stop octomate.service` |
| Disable automatic startup | `systemctl --user disable --now octomate.service` |

The CLI's `service start`, `stop`, `restart` and `upgrade` commands still manage
macOS LaunchAgents. Use systemd here, and follow the
[manual upgrade procedure](upgrading.md) for release changes and database backups.
