# Installation

An installation has two halves.

- The **server** runs Octomate: the database, the channel connections, the agents it
  drives, and the web console. One per deployment. The `octomate` package.
- The **client** lives wherever you run a coding agent yourself. It forwards that
  session's hooks and transcript to the server and gives the agent Octomate's tools
  over MCP. One per machine. The `octomate-cli` package, which installs alone.

They can share one computer. A laptop can also send to a server elsewhere, but the
transcript tail stays on the laptop, because that is where the transcript files are.

[Requirements](requirements.md) lists what the server host needs.
[Quick start](quickstart.md) is the short version of the whole tab; the
[brief for your agent](agent-setup.md) is the same thing written for an assistant to
carry out.

## Choose a server path

| Host | Setup | Supervision | Where it stops today |
|---|---|---|---|
| macOS | `octomate service init --prepare` | A GUI LaunchAgent the CLI manages: `service start`, `stop`, `status`, `logs`, `verify`, `upgrade` | Activation is a manual copy of the generated plist. The service runs only while that desktop account is logged in. |
| Linux | Source checkout, explicit config | A systemd user unit you write from the recipe | No CLI service installer and no managed upgrade. |
| Windows | WSL 2 | The Linux recipe inside WSL | No native Windows service, and no native transcript tail: the client must run in WSL too. |
| Docker | `docker compose up -d` | Docker's restart policy | Driven agents need their harness logins inside the container. |

The `service` commands other than `serve`, `invite` and `user` require macOS. On
every platform, `octomate service serve` runs the server in the foreground.

## Installation is complete when

- The database path is the one you intended, and its schema is current.
- You can sign in to the console, issue a token, and see a native session arrive.
- An unauthenticated request to `/hooks/...` and `/octomate/mcp` gets a 401.
- A channel receives one message, runs the entry agent, and returns a reply.
- A read-only MCP call, `gateway_scry` with `reveal: "routes"`, works as your account.
- Restarting the service keeps configuration and stored history.

[Configuration](configuration.md) explains the directory layout and the difference
between server settings and client credentials.
