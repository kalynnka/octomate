# Docker

The deployment uses two images, started together with Docker Compose:

| Container | Responsibility | Persistent files |
|---|---|---|
| `octomate` | Server, agents, channel connections and storage; includes Git, Node.js and the locked Claude/Codex executables | `state/` and `agent-home/` |
| `trunkline` | Web UI and Nginx proxy to the server | None |

Your browser and native clients use the same host address. Trunkline forwards
API, OAuth, hook and MCP requests to Octomate over the Compose network, including
streaming responses and WebSocket connections. Only Trunkline publishes a host
port; the server's port stays internal. The web container has no credentials or
database mounts.

!!! info "Deployment support"
    **Windows users: Docker is the recommended deployment path.** Sorry, we don't
    have a native Windows installer yet. Use the [PowerShell setup below](#windows)
    with Docker Desktop running Linux containers.

    The interactive wizard runs on macOS and Linux. The Windows Docker steps
    have not been tested end to end; macOS native setup remains the only flow
    tested end to end with live agents.

## 1. Prepare the deployment

### macOS and Linux: use the wizard

On the host, install Git, uv and a running Docker engine with Compose **2.24.0 or
newer**. Node.js and the agent executables are built into the image, so they do
not need installing on the host for driven sessions.

```sh
uv tool install octomate-cli
octomate service init --prepare --target docker
```

Choose a new directory, port, Claude or Codex, and Trunkline. The wizard builds
both images, runs the configuration generator in a temporary server container, and checks
the result. It does not start Octomate, create a database or copy host credentials.
Select Claude or Codex for this image. DSH needs a custom image with its executable;
add it to the configuration after preparation.

For a version that has not shipped yet, run from your development checkout:

```sh
uv run --no-sync octomate service init --prepare --target docker --source "$PWD"
```

Choose an installation outside that checkout. The prepared layout is:

```text
<installation>/
  app/                 source used to build both images
  compose.yaml         two services, host port and persistent mounts
  state/
    config/            seven YAML configuration files
    .env               generated salts; optional source of runtime variables
    CONFIGURATION.md   configuration checklist
    octomate.db        created later, during database initialisation
    .octomate/         working data and project workspaces
  agent-home/          persistent container home, including agent logins
  logs/prepare.log     image build and configuration output
```

Run the remaining commands **from the installation directory**. The generated
Compose file builds and runs the server account with your host UID/GID so its
private bind mounts stay writable.
It maps `state/` to `/data` and `agent-home/` to `/home/octomate`. Moving the setup
to another host may require updating the UID/GID build arguments, `user:` and
directory ownership, then rebuilding the image.

### Windows: use Docker Compose { #windows }

Install Git and [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/),
checking its Windows edition and virtualization requirements. Start Docker Desktop
in **Linux-container mode**, then use PowerShell. Python, Node.js and the agent
executables run inside the containers.

The native `octomate service init` wizard does not run on Windows yet. Use the
same configuration generator inside the server container instead:

```powershell
git clone https://github.com/kalynnka/octomate.git octomate-server
Set-Location octomate-server
New-Item -ItemType Directory state, agent-home
docker compose build
docker compose run --rm octomate python -m octomate_cli.deployment prepare --target docker --port 8000 --agent codex --channel trunkline
```

Use a checkout that includes the deployment files described here. Replace
`--agent codex` with `--agent claude`, or repeat the option for both.
These commands prepare a new installation; they do not create a database or
start the server. The generator refuses to overwrite existing configuration.

For this path, `docker-compose.yml`, `state/` and `agent-home/` live directly in
the checkout rather than in the wizard's separate installation directory. Keep
both data directories when updating or replacing the checkout. Leave the supplied
container UID/GID at `1000:1000`; Windows accounts do not use Unix UIDs.

Continue below from this PowerShell directory: review configuration, authenticate
agents inside Docker, then initialise the database and start both containers.
Use `docker compose logs` to inspect startup and verify a real agent reply before
relying on unattended operation.

## 2. Review configuration

Complete `state/CONFIGURATION.md`. Review the [settings](settings.md) and
[tentacles](tentacles.md), using the
[packaged YAML templates](server.md#or-start-from-the-templates) as a reference.
Leave the server's `tentacles.trunkline.static_dir` unset. The separate Trunkline
container serves the compiled UI; its API remains part of the Octomate server.
For an installation prepared before this split, remove an existing
`static_dir: /app/trunkline/dist` entry when switching to the new images. Update
its Compose file with the separate `trunkline` service and move the host port
mapping there, preserving your paths, port and server UID/GID. Rerunning
`init --prepare` validates an existing installation; it does not rewrite it.

The server listens on `0.0.0.0:8000` **inside the Compose network**. Trunkline
listens on port 8080 in its container; Compose publishes that as
`127.0.0.1:<your-port>` on the host. Use this host address for browser access,
native clients and OAuth callbacks. Set `auth.cookie_secure: false` in `state/config/auth.yaml`
for the initial local HTTP session.

Provide Octomate secrets in YAML or container environment variables. Compose
also loads `state/.env` if present; you can use another `env_file` or your own
environment injection. Host shell variables are not automatically forwarded.
YAML values take precedence, so remove matching placeholders when using environment
variables. See [Compose environment files](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

```sh
docker compose run --rm octomate python -m octomate_cli.deployment check
```

## 3. Authenticate the agents { #agent-credentials }

**Log in inside the container**, using its persistent home. A login on your
laptop does not automatically authenticate the container. Rebuilding or replacing
the container preserves `agent-home/`; deleting that directory removes the
stored logins.

### Claude

```sh
docker compose run --rm octomate claude auth login
docker compose run --rm octomate claude auth status
```

Follow the printed browser instructions. On Linux, Claude stores its login under
`~/.claude/`, which maps to `agent-home/.claude/` here. On macOS, credentials may
live in Keychain, so copying `~/.claude` alone may not transfer a working login.
See [Claude authentication](https://code.claude.com/docs/en/authentication).

For unattended authentication, explicitly provide `ANTHROPIC_API_KEY` or a
`CLAUDE_CODE_OAUTH_TOKEN` generated with `claude setup-token` in the **container's
environment**. These are Claude runtime variables, not fields in Octomate's YAML.
Keep them out of the Dockerfile and image build arguments. API-key usage follows
the provider's API billing; it does not reuse a subscription login.

### Codex

```sh
docker compose run --rm octomate codex -c 'cli_auth_credentials_store="file"' login --device-auth
docker compose run --rm octomate codex login status
```

Complete the device-code flow in your browser. It may need enabling in your
account or workspace settings. The file-backed cache lives in
`agent-home/.codex/auth.json`. See [Codex authentication](https://developers.openai.com/codex/auth/).

If you already have a file-backed `auth.json`, you may copy it into that location
yourself. Keep it private and writable by the container user so refreshed tokens
can be saved. An OS keyring login cannot be transferred by copying an absent or
stale file. A separate container login avoids sharing a refreshable credential
file with another running agent.

For API-key login, pass `OPENAI_API_KEY` into a one-off container and use stdin:

```sh
docker compose run --rm -e OPENAI_API_KEY octomate sh -c 'printenv OPENAI_API_KEY | codex login --with-api-key'
```

Set the variable through your preferred secret source first; do not paste its
value into the command. This saves the login in the persistent Codex home.

### Repository access

Model login and Git authentication are separate. Private repositories need
credentials available inside the container as well. Configure a dedicated Git
credential or narrowly scoped SSH mount yourself; the image does not mount your
host's SSH directory or Docker socket. Project paths refer to container paths.

## 4. Initialise the database and start

Confirm that `/data/octomate.db` maps to this installation's
`state/octomate.db`. These commands **write that database**; use them for a new
installation. Back up existing data before applying migrations.

```sh
docker compose run --rm octomate alembic -c /app/octomate/migrations/alembic.ini upgrade head
docker compose run --rm octomate alembic -c /app/octomate/migrations/alembic.ini current
docker compose up -d
docker compose logs --tail 100 octomate trunkline
```

Startup does not apply migrations. Create an invitation, substituting your host
port if different; this command writes the invitation to the same database:

```sh
docker compose exec octomate octomate service invite --url http://127.0.0.1:8000
```

Open the registration link, register and sign in. Follow
[Accounts and tokens](accounts.md#issue-a-client-token) for client credentials.
Send a simple request to each selected agent through Trunkline, then repeat after
`docker compose restart` and confirm that history remains.

## 5. Operate the service

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Follow logs | `docker compose logs -f octomate` |
| Follow web/proxy logs | `docker compose logs -f trunkline` |
| Restart | `docker compose restart octomate` |
| Stop | `docker compose stop octomate` |
| Rebuild after an intentional source update | `docker compose build` |
| Recreate with the new image | `docker compose up -d` |

To update only the UI, run `docker compose build trunkline`, then
`docker compose up -d --no-deps trunkline`. The server and its agent processes
keep running. Restarting or rebuilding either container preserves the host's
`state/` and `agent-home/` directories.

Before an upgrade, stop the service and back up `state/` together with
`agent-home/`. Preserve the database, auth salts and OAuth key together. Review
the release's migrations before recreating the service; rebuilding alone does
not update the schema. See [Upgrades and backups](upgrading.md).

Use [Tailscale](networking.md#tailscale) or another private connection for remote
access. Public internet exposure is not recommended, even with Octomate login.
Connect native agents using the [client quick start](clients/quickstart.md);
their hooks run on the machine where the native session lives.
