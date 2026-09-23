# Deployments

Choose how to run your Octomate server. Each guide covers preparation,
configuration and the first working session. Check the
[requirements](requirements.md) before you begin, or give your agent the
[Quickstart brief](quickstart.md#agent-tldr) to guide the setup.

<div class="grid cards" markdown>

- **[macOS](macos.md)**

    Run Octomate under your desktop account, with access to your existing agent
    logins. The CLI wizard prepares the installation and a GUI LaunchAgent for
    running it while you are logged in.

    [Install on macOS](macos.md)

- **[Linux](linux.md)**

    Run Octomate on a Linux host under a dedicated service account or your own
    user. The CLI wizard prepares the configuration and a systemd user service.

    [Install on Linux](linux.md)

- **[Docker](docker.md)**

    Run the server and Trunkline in separate containers with Docker Compose.
    Keep data and agent logins in persistent host directories. **Recommended
    for Windows users**, with a PowerShell setup guide.

    [Deploy with Docker](docker.md) · [Windows setup](docker.md#windows)

- **[Manual setup](server.md)**

    Prepare a source checkout and run the server in the foreground. Use the
    shared configuration generator or the YAML templates, then manage the
    process with your own tooling.

    [Set up manually](server.md)

</div>

!!! info "Deployment support"
    **macOS is the only deployment flow tested end to end with live agents.**
    The wizard also prepares Linux and Docker installations. On Windows, use
    [Docker Compose](docker.md#windows). Sorry, native Windows deployment isn't
    available yet. The CLI's managed start/stop/upgrade commands still require macOS;
    Linux uses systemd and Docker uses Compose.

Once your server is running, [create your account](accounts.md) and
[connect your native agents](clients/quickstart.md). For access from another
device, use [Tailscale or another private connection](networking.md#tailscale).
