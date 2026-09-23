# Connect your native agent

On the machine where you run Claude Code, Codex or DeepSeek Harness. You need the
server's base URL and an API token with the `hooks` and `mcp` scopes, issued in
Trunkline: see [Accounts and tokens](../accounts.md#issue-a-client-token).

Install the client and point it at the server. On the server host itself the
operator CLI you already installed is the same package, so skip the first line:

```sh
uv tool install octomate-cli
octomate configure --url https://octomate.example.com --token '<api-token>'
```

The token is your Octomate API key, not your model provider's key or harness
login. Use the server's base URL; the installers add the hook and MCP paths.

## Install hooks and MCP

Run only the tab for the agent you use. These installers add Octomate's entries
while preserving other hooks and settings.

=== "Claude Code"

    ```sh
    octomate claude hooks install
    octomate claude mcp install --scope user
    octomate claude mcp show --scope user
    ```

    This installs user hooks and makes the MCP entry available across your
    projects. For MCP in just one project, run `octomate claude mcp install`
    from that project directory instead; its default scope is `local`.
    [Claude Code settings and scopes](claude-code.md).

=== "Codex"

    ```sh
    octomate codex hooks install
    octomate codex mcp install
    octomate codex mcp show
    ```

    Open `/hooks` in Codex and trust the new command hooks. Installing the file
    does not grant that trust. [Codex setup](codex.md).

=== "DeepSeek Harness (experimental)"

    ```sh
    octomate deepseek hooks install --bridge /path/to/deepseek-harness/packages/hooks/hooks-claude-code
    octomate deepseek mcp install
    octomate deepseek mcp show
    ```

    Replace the bridge path with your local checkout. Read the
    [DeepSeek guide](deepseek.md) for the native gateway URL and launch token
    required by its event tail.

Restart the runtime so it reads its settings. `mcp show` checks the saved entry;
the runtime's own MCP status tells you whether it connected. MCP entries embed
your token, so reinstall them with the same scope after a URL or token change.

## Verify both integrations

Send one distinctive prompt in a fresh session. Its prompt and answer should appear
as a thread in Trunkline. Then ask the agent to call `gateway_scry` with
`reveal: "routes"`, which is read-only and proves the MCP entry.
Seeing a prompt without its answer means collection is incomplete; inspect the
tail and WebSocket connection. [Hooks and MCP](index.md) covers configuration
precedence, rotation and troubleshooting.
