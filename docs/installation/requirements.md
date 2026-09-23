# Requirements

For a first installation, use one coding agent and Trunkline. External chat bots,
an OAuth application and a public hostname can all be added later.

| Requirement | Where it is needed | Why |
|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git | Server; uv also on native client machines | Install the CLI, checkout and dependencies |
| Python 3.12 or newer | Server | The project minimum; the manual recipe selects Python 3.13 through uv |
| Your coding harness and its login | Each machine where that agent runs | Native sessions use the client machine's login; driven runs use the server account's login |
| Node.js 22.12 or newer and pnpm | The machine building Trunkline | Build the web console; the Python API can run without the frontend |

The installation uses uv to provision Python and the locked dependencies.
The Node minimum follows the frontend's locked build dependencies.
Log in to the harness **as the account
the server runs under**, without `sudo`. A laptop login does not authenticate a
server on another machine.

The three distributions release independently: `octomate` (server, includes the CLI),
`octomate-cli` (client), `octomate-protocol` (the shared contract both depend on).
The transcript stream checks protocol compatibility when it connects. Keep the
operator CLI as a standalone uv tool: managed macOS upgrades refuse to run from
the service's own environment. Continue with the [macOS guide](macos.md) or
[Manual setup](server.md).
