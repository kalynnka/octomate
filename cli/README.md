# Octomate CLI

Connect native coding agent sessions to an Octomate server. This package provides
hook installation, MCP configuration, and transcript streaming for Claude Code,
Codex, and DeepSeek Harness.

```bash
pip install octomate-cli
octomate configure --url https://your-server.example
octomate claude hooks install
octomate --version
```

Python 3.12 or newer is required. Installing the CLI also installs
`octomate-protocol`; it does not install the server or its dependencies.

All commands remain available in help. `octomate serve` needs the separately
installed `octomate` server package. `octomate upgrade` manages an existing
launchd/plist deployment checkout and installs the latest stable release; it
does not update a client-only CLI installation. Update this package with the
same installer used to install it, such as `pip install --upgrade octomate-cli`.

See the [project documentation](https://github.com/kalynnka/octomate) and
[server deployment guide](https://github.com/kalynnka/octomate/blob/main/docs/server-deployment.md).
