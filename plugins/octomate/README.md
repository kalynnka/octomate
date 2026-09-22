# Octomate for Codex

This plugin bundles Octomate's MCP connection and native Codex hooks. It uses
the existing CLI configuration for the server URL and API token. The plugin
contains no credentials or machine-specific paths.

## Install from a checkout

From the Octomate repository root, install the CLI with its plugin dependencies:

```sh
uv tool install --editable './cli[codex-plugin]'
octomate configure --url http://your-server:8000 --token '<api-token>'
codex plugin marketplace add .
codex plugin add octomate@octomate
```

The token needs `hooks` and `mcp` scopes. Ensure the uv tool bin directory is on
the PATH inherited by Codex. Open `/hooks` in Codex and trust the plugin's command
hooks, then start a new session.

The existing `octomate codex mcp install` and `octomate codex hooks install`
commands remain available. Use either those registrations or the plugin for a
given session. If switching to the plugin, remove the old Octomate registrations:

```sh
octomate codex mcp uninstall
octomate codex hooks uninstall
```

Also remove any project-scoped Octomate hooks with
`octomate codex hooks uninstall --scope project` in the relevant project.
These commands preserve unrelated registrations.

## Connection and tracking

The MCP command bridges stdio to the configured `/octomate/mcp` endpoint,
preserving the server's instructions, tools, notifications, and results. It sends
the configured bearer and `X-Octomate-Client: codex-native` on the HTTP connection.
The optional `codex-plugin` dependency group supplies the MCP transport libraries;
ordinary CLI installations do not need them.

Hooks use the same emit and transcript-launch code as the direct CLI installer:
`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStart`, and `SubagentStop`.
The launcher starts the existing Codex transcript tail on session start, prompt
submission, and subagent completion.

Both integrations resolve settings from `OCTOMATE_CLI_URL` and
`OCTOMATE_CLI_TOKEN`, then the project's `.octomate/cli.toml`, then the user's
`~/.config/octomate/cli.toml`. The MCP connection resolves configuration when it
starts; hooks resolve it when they fire. Start a new Codex session after changing
the configuration so its MCP connection and hooks agree.

The plugin is for native Codex sessions. Octomate-driven sessions keep their
existing isolated integrations. Claude's installation is unchanged.
