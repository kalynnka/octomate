# Octomate CLI

Connect native coding agent sessions to an Octomate server. This package provides
hook installation, MCP configuration, and transcript streaming for Claude Code,
Codex, and DeepSeek Harness.

```bash
pip install octomate-cli
octomate configure --url https://your-server.example --token '<api-token>'
octomate claude hooks install
octomate --version
```

Python 3.12 or newer is required. Installing the CLI also installs
`octomate-protocol`; it does not install the server or its dependencies.
Package versions are independent. A compatible server update does not require
updating the CLI; stream connections check the shared wire protocol version.

All commands remain available in help. The foreground runner,
`octomate service serve`, requires the separately installed `octomate` server package. Update this package with the same installer
used to install it, such as `pip install --upgrade octomate-cli`.

See the [project README](https://github.com/kalynnka/octomate).

## Client authentication

Create an invitation on the server, then register using that code:

```sh
octomate service invite
octomate service user create --username alice --password 'YourPassword1!' --invitationcode '<code>'
```

These commands require the server package and use its configured database.
`octomate service invite --url <server>` prints a registration link instead of the code.
Sign-in happens in the Trunkline UI.

Issue an API token through the authenticated account API, then run
`octomate configure --token <api-token>`. The config key is `token` and the
environment override is `OCTOMATE_CLI_TOKEN`. Old `secret` values and
`OCTOMATE_CLI_SECRET` are no longer used. Install hooks and MCP after configuring
the token, and reinstall MCP entries when it changes.
