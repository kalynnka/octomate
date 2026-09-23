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
`$DSH_API_URL` or `http://127.0.0.1:3080`. An authenticated dsh needs its launch
token in the tail's environment: set `DSH_LAUNCH_TOKEN`, or use the complete launch
URL with its `?token=` as `DSH_API_URL`. The hook process must inherit it too.

Octomate's own driven dsh child listens on port 3081, separate from the native
harness on 3080.
