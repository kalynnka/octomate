# Windows

Run the server **and the agents whose sessions it records** inside WSL 2. Two
reasons: the CLI has no Windows service adapter, and the transcript tail relies on
POSIX file locks, so `octomate <runtime> tail` does not run on native Windows. A
Claude Code or Codex installed on the Windows side cannot be recorded by a client
inside WSL either: they have different homes and different transcript paths.

## Prepare WSL

In an administrator PowerShell:

```powershell
wsl --install -d Ubuntu
wsl --version
```

Create your Linux user, then inside the distribution install git, uv and your
harness. Keep the installation and your projects under the Linux home, for example
`~/.local/share/octomate-server` and `~/Projects`, so every process sees one
filesystem with Linux paths. Register projects by their Linux paths.

Follow [Server setup](server.md) inside WSL, and install [hooks and
MCP](clients/index.md)
in the same distribution.

## Run it

```sh
cd "$OCTOMATE_INSTALL_ROOT"
export OCTOMATE_HOME="$OCTOMATE_INSTALL_ROOT/config"
export OCTOMATE_DB_URL="sqlite+aiosqlite:///$OCTOMATE_INSTALL_ROOT/octomate.db"
"$OCTOMATE_INSTALL_ROOT/app/.venv/bin/octomate" service serve
```

Test `http://localhost:8000` from a Windows browser and the loopback URL inside WSL.
If only the latter works, it is WSL networking, not the bind address; Microsoft's
[networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking) covers it.

For supervision, check `systemctl status` in the distribution. If systemd is off,
add to `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then `wsl --shutdown` from PowerShell, reopen the distribution, and follow the
[Linux unit recipe](linux.md). Microsoft's
[systemd guide](https://learn.microsoft.com/en-us/windows/wsl/systemd) has the
prerequisites.

!!! note "WSL lifetime"
    A systemd unit starts when the distribution starts, and nothing starts the
    distribution at Windows boot or keeps it alive on its own. Verify what happens on
    login, on closing the terminal, on sleep and on reboot. For an unattended server,
    a Linux host or the macOS setup has a clearer lifecycle.

A separate Linux or macOS server is the other option: the WSL client streams its
sessions there over HTTPS.
