# Quick start

Install the operator CLI with [uv](https://docs.astral.sh/uv/getting-
started/installation/):

```sh
uv tool install octomate-cli
octomate --version
```

Then pick a server path:

=== "macOS"

    Prepare an isolated installation with the wizard, complete its checklist, then
    activate it as a GUI LaunchAgent:

    ```sh
    octomate service init --prepare
    ```

    Follow [macOS](macos.md). Preparation writes files only; the database, your
    account and the service are the steps after it.

=== "Linux"

    Clone a release, sync its locked dependencies, and run the foreground server
    under systemd. Follow [Server setup](server.md) then [Linux](linux.md).

=== "Windows"

    Run the server and the native agents inside WSL 2, then follow the Linux recipe.
    See [Windows](windows.md).

=== "Docker"

    `docker compose up -d` from a checkout. See [Docker](docker.md).

Once the server answers and you have [an account and an API token](accounts.md),
connect each machine where an agent runs:

```sh
octomate configure --url https://octomate.example.com --token '<api-token>'
octomate claude hooks install
octomate claude mcp install
```

Use the `codex` or `deepseek` command groups for those runtimes.
[Hooks and MCP](clients/index.md) covers scopes, restarts and verification.

**Prefer your agent to do the setup?** Give it the [installation brief](agent-setup.md).
