# Requirements

For a first installation, use one coding agent and Trunkline. External chat bots,
an OAuth application and a public hostname can all be added later.

| Requirement | Where it is needed | Why |
|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Native server, macOS/Linux wizard host and native client machines | Install the CLI and Python dependencies |
| Git | Native server or Docker build host | Check out the source |
| Python 3.12 or newer | Native server; included in Docker | The project minimum; the manual recipe selects Python 3.13 through uv |
| Your coding harness and its login | Each machine where that agent runs | Native sessions use the client machine's login; driven runs use the server account's login |
| Node.js 22.12 or newer and pnpm | Native host building Trunkline; included in the Docker build | Build the web console; the Python API can run without the frontend |
| Docker engine and Compose 2.24.0 or newer | Docker build and runtime host | Build the image and manage persistent mounts |

The installation uses uv to provision Python and the locked dependencies.
The Node minimum follows the frontend's locked build dependencies.
Log in to the harness **as the account
the server runs under**, without `sudo`. A laptop login does not authenticate a
server on another machine. Docker agents authenticate inside the container using
its [persistent agent home](docker.md#agent-credentials).

The three distributions release independently: `octomate` (server, includes the CLI),
`octomate-cli` (client), `octomate-protocol` (the shared contract both depend on).
The transcript stream checks protocol compatibility when it connects. Keep the
operator CLI as a standalone uv tool: managed macOS upgrades refuse to run from
the service's own environment. Continue with [macOS](macos.md), [Linux](linux.md),
[Docker](docker.md) or [Manual setup](server.md). For Windows, we recommend
[Docker Desktop with the PowerShell setup](docker.md#windows). Sorry, there is no
native Windows deployment option yet; the Docker path does not need host Python
or uv for server preparation.
