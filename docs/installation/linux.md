# Linux

Run the foreground server under a systemd user unit. This is an operator recipe:
the CLI has no Linux service installer and no managed upgrade, so `octomate service
serve` is the whole of what it contributes here.

## Bootstrap

Install Python, git, uv and your harness under the account that will run the
server, and log the harness in as that account. Follow [Server setup](server.md)
with a dedicated installation root, verify a foreground run, an account and one
driven reply, then stop that process.

The unit runs with no shell profile, so anything a harness needs from your shell,
`PATH` entries or provider variables, goes into the unit.

## Install a systemd user service

`~/.config/systemd/user/octomate.service`, with your own paths:

```ini
[Unit]
Description=Octomate

[Service]
Type=simple
WorkingDirectory=/home/alice/.local/share/octomate-server
ExecStart=/home/alice/.local/share/octomate-server/app/.venv/bin/octomate service serve
Environment=OCTOMATE_HOME=/home/alice/.local/share/octomate-server/config
Environment=OCTOMATE_DB_URL=sqlite+aiosqlite:////home/alice/.local/share/octomate-server/octomate.db
Environment=PATH=/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=60
UMask=0077

[Install]
WantedBy=default.target
```

`.env` stays in the working directory; the server reads it itself. Add the directory
holding `claude`, `codex` or `dsh` to `PATH`. `KillMode=control-group` makes a stop
take the agent subprocesses with it.

```sh
systemd-analyze --user verify "$HOME/.config/systemd/user/octomate.service"
systemctl --user daemon-reload
systemctl --user enable --now octomate.service
systemctl --user status octomate.service
journalctl --user -u octomate.service -f
```

The unit starts whatever release is installed. It never syncs packages or migrates.

## Keep it running after logout

A user unit lives and dies with the user's session manager. On an always-on host,
enable lingering for the account:

```sh
sudo loginctl enable-linger alice
loginctl show-user alice -p Linger
```

Verify after a real reboot, then repeat the [HTTP checks](server.md#run-and-verify)
and one driven reply.

## Upgrading

Stop the unit, snapshot the database, check out the release, sync, rehearse the
migration on a copy, migrate, rebuild the console, start. The exact commands are in
[Upgrades and backups](upgrading.md#linux-and-docker). Do not use the macOS
`octomate service upgrade` here.
