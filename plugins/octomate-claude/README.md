# Octomate for Claude Code

This plugin bundles Octomate's MCP connection and native Claude Code hooks.
The repository has a root
`.claude-plugin/marketplace.json` catalog pointing to a separate plugin directory.
The plugin uses the installed Octomate CLI and contains no credentials or
machine-specific paths.

## Install from a checkout

From the Octomate repository root:

```sh
uv tool install --editable './cli[claude-plugin]'
octomate configure --url http://your-server:8000 --token '<api-token>'
claude plugin marketplace add .
claude plugin install octomate@octomate --scope user
```

If using both native plugins, install `./cli[codex-plugin,claude-plugin]` instead.
The token needs `hooks` and `mcp` scopes. Ensure the uv tool bin directory is on
the PATH inherited by Claude Code, review the plugin in `/plugin`, and start a
new session. The plugin does not install the CLI itself.

Claude supports three installation scopes. Use `--scope user` for every project,
`--scope project` to record the plugin in the project's `.claude/settings.json`,
or `--scope local` for this project only in `.claude/settings.local.json`. Run
project/local installs from the target project after registering the marketplace.
Registering a local marketplace path does not by itself restrict plugin scope.
See [Claude's plugin scopes](https://code.claude.com/docs/en/plugins-reference#plugin-installation-scopes).

## Connection and tracking

Claude starts `octomate-claude-mcp`, which forwards MCP over HTTP to the configured
`/octomate/mcp` endpoint. It shares the Codex plugin's transport implementation
but sends `X-Octomate-Client: claude-native`. Server instructions, including the
hint to search Octomate's proxied MCPs, pass through the bridge.

The hooks invoke the existing lightweight `octomate-emit` and `octomate-launch`
entry points. They forward `UserPromptSubmit`, `Stop`, `SessionEnd`,
`SubagentStart`, and `SubagentStop` to `/hooks/claude`. The transcript launcher
runs on `UserPromptSubmit`, matching the direct Claude hook installer.

MCP and hooks resolve settings from `OCTOMATE_CLI_URL` and `OCTOMATE_CLI_TOKEN`,
then the working directory's `.octomate/cli.toml`, then the user's
`~/.config/octomate/cli.toml`. This settings precedence is separate from plugin
installation scope. The MCP bridge reads settings at startup; hooks read them
when they fire. Start a new session after changing settings.

## Switching from direct installation

The existing `octomate claude mcp install` and `octomate claude hooks install`
commands remain available. When switching to the plugin, uninstall Octomate's
direct registrations at the scopes where you installed them to avoid duplicate
connections and event delivery:

```sh
octomate claude hooks uninstall --scope user
octomate claude mcp uninstall --scope local
```

Those examples use the direct installers' defaults. Hook uninstall also accepts
`--scope project`; MCP uninstall also accepts `--scope user` and `--scope project`.
Run project/local removal in each affected project. Other integrations are kept.
Octomate-driven Claude sessions retain their existing isolated integration.
