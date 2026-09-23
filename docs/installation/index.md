# Installation

Start with **one agent and Trunkline**, the web console. Get a native session into
history and a driven reply back from the console, then add your chat platforms and
MCP connectors.

[Copy the agent setup brief](quickstart.md#agent-tldr){ .md-button .md-button--primary }
[Deploy on macOS yourself](macos.md){ .md-button }

## What you install

An installation has two halves.

- The **server** runs Octomate: the database, the channel connections, the agents it
  drives, and the web console. One per deployment. The `octomate` package, best
  installed from a release checkout.
- The **client** lives wherever you run a coding agent yourself. It forwards that
  session's hooks and transcript to the server and gives the agent Octomate's tools
  over MCP. One per machine.

Both halves use the same command. `octomate-cli` is one package with two jobs: on
the server host it is the operator tool that prepares, starts and upgrades the
service; on an agent's machine it is the client that installs hooks and the MCP
entry. It is installed once per machine, as a standalone uv tool.

They can share one computer. A laptop can also send to a server elsewhere, but the
transcript tail stays on the laptop, because that is where the transcript files are.

Start with the [server quick start](quickstart.md), then the
[client quick start](clients/quickstart.md). The [Agent TL;DR](quickstart.md#agent-tldr)
that opens the quick start is both halves, written for an assistant to carry out.

## Choose a server path

Read the [requirements](requirements.md), then explore the
[deployments](deployment.md) for macOS, Linux, Docker and manual setup.
For Windows, we recommend [Docker Compose](docker.md#windows).

## Follow the setup in order

1. Give your agent the [Quickstart](quickstart.md) brief, or choose a
   [deployment guide](deployment.md) to prepare the server yourself.
2. Review the [config home](configuration.md), [server settings](settings.md) and
   [tentacle registry](tentacles.md), in that order.
3. [Create your account and issue a client token](accounts.md).
4. [Connect your native agent](clients/quickstart.md) with hooks and MCP.
5. Add an [external channel](../usage/channels/index.md),
   [project](projects.md) or [MCP connector](../tentacles/mcp.md) when
   you need it. Use [Tailscale or another private connection](networking.md#tailscale)
   for access from other devices. Public internet exposure is not recommended,
   even with Octomate's built-in login enabled.

## Installation is complete when

- The database path is the one you intended, and its schema is current.
- You can sign in to the console, issue a token, and see a native session arrive.
- An unauthenticated request to `/hooks/...` and `/octomate/mcp` gets a 401.
- A channel receives one message, runs the entry agent, and returns a reply.
- A read-only MCP call, `gateway_scry` with `reveal: "routes"`, works as your account.
- Restarting the service keeps configuration and stored history.

[Configuration](configuration.md) explains the directory layout and the difference
between server settings and client credentials.
