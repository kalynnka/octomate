# Requirements

- Python 3.12 or newer, uv, and git on the server.
- The coding agents you want to drive, installed and logged in **as the account the
  server runs under**. A login on your laptop does not authenticate a server process.
- SQLite with FTS5, which every current Python build ships. There is no database
  server to stand up.
- Node.js and pnpm to build the web console. The API runs without it.

The three distributions release independently: `octomate` (server, includes the CLI),
`octomate-cli` (client), `octomate-protocol` (the shared contract both depend on).
A compatible server update never requires a client upgrade. Keep the operator CLI as
a standalone uv tool: managed macOS upgrades refuse to run from the service's own
environment.
