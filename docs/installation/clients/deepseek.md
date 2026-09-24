# DeepSeek Harness (experimental)

Configure the server URL and your API token first with the
[client quick start](quickstart.md). These steps connect a native dsh session;
they are separate from the server's driven dsh runtime.

dsh has no hook protocol of its own. Octomate's hooks ride a bridge plugin that
speaks Claude Code's dialect, and the bridge ships outside dsh's bundle, so the
first install links it:

```sh
octomate deepseek hooks install \
  --bridge /path/to/deepseek-harness/packages/hooks/hooks-claude-code
octomate deepseek mcp install
octomate deepseek mcp show
```

Both write into the dsh home: `--home`, else `$DSH_HOME`, else `~/.dsh`. Hooks get
their own `octomate-hooks.json` and a marker-delimited row in `cordis.patch.yml`;
the MCP entry is a second row in the same file. Only `UserPromptSubmit` and `Stop`
are registered, because the bridge delivers nothing else. **Restart every dsh
process, the web daemon included**, after installing.

The DeepSeek tail does not read a file. dsh's log is compressed in frames that only
advance at checkpoints, so the tail polls the local dsh gateway instead, at
the gateway URL saved as `deepseek.url` in the client configuration. After starting
your native dsh web gateway, save its complete launch URL, including `?token=`:

```sh
octomate configure --dsh-url 'http://127.0.0.1:3080/?token=YOUR_DSH_LAUNCH_TOKEN'
```

Run this from any directory for user-wide configuration. It writes
`~/.config/octomate/cli.toml` with owner-only permissions and does not print the
launch URL. Use `--scope project` from the session's working directory to store it
in `.octomate/cli.toml` instead. Update it when the gateway address or token changes.
The saved gateway setting is scoped to DeepSeek:

```toml
[deepseek]
url = "http://127.0.0.1:3080/?token=YOUR_DSH_LAUNCH_TOKEN"
```

Keep the gateway running while native sessions stream.

DeepSeek removes `DSH_*` variables from hook subprocesses, so exported
`DSH_API_URL` and `DSH_LAUNCH_TOKEN` alone do not configure automatic ingestion.
Manually launched tails still accept those variables and `--dsh-url`. The URL
precedence is `--dsh-url`, `DSH_API_URL`, the client configuration, then
`http://127.0.0.1:3080`.

Octomate's own driven dsh child listens on port 3081, separate from the native
harness on 3080.
