# Codex

```sh
octomate codex hooks install
octomate codex mcp install
octomate codex mcp show
```

Hooks go to `~/.codex/hooks.json` by default, or `./.codex/hooks.json` with
`--scope project`, or wherever `--hooks-file` points. The events are `SessionStart`,
`UserPromptSubmit`, `Stop`, `SubagentStart` and `SubagentStop`, with the launcher on
the first two and on `SubagentStop`, which is the hook that knows a child
rollout's path.

**Open `/hooks` in Codex and trust the new command hooks.** Codex does not run
untrusted hooks, so installing them on disk is not enough.

The MCP entry is `[mcp_servers.octomate]` in `~/.codex/config.toml`, or the file
`--config-file` names. The installer round-trips the file with its comments intact.
Codex lists the tools under `mcp__octomate`.
