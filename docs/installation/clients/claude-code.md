# Claude Code

```sh
octomate claude hooks install
octomate claude mcp install
octomate claude hooks show
octomate claude mcp show
```

The hooks installer merges Octomate's handlers into `~/.claude/settings.json`, keeping
every other hook. It registers `UserPromptSubmit`, `Stop`, `SessionEnd`,
`SubagentStart` and `SubagentStop`, and adds the transcript launcher to
`UserPromptSubmit`. `SessionStart` is deliberately absent: the first prompt starts
the session, so a handler there would spawn a process for nothing. Re-running
replaces a stale Octomate handler in place. Restart Claude Code so it reads the file.

| Option | Effect |
|---|---|
| `--scope user` (default) | `~/.claude/settings.json` |
| `--scope project` | `./.claude/settings.json` |
| `--settings <path>` | An explicit file |
| `--url <hook url>` | Pin the **full** hook endpoint, `https://host/hooks/claude`. Without it, the hook resolves the base URL when it fires. |

The MCP entry has three placements. The default is the narrowest, since the entry
embeds the token:

| Scope | File | Where in it |
|---|---|---|
| `local` (default) | `~/.claude.json` | This directory's entry only |
| `user` | `~/.claude.json` | Every project |
| `project` | `./.mcp.json` | Committed alongside the code, so keep it out of git |

`uninstall` and `show` take the same scope. The entry is an `http` server named
`octomate` with an `Authorization` header and `X-Octomate-Client: claude-native`.
Claude lists its tools as `mcp__octomate__<tool>`.
