# Hooks and MCP

Install these on the machine where the native agent runs. The server can be
elsewhere: it never reads a transcript from disk, not even on its own machine. What
it receives is what the client sends.

Two integrations, installed separately:

- **Hooks** record sessions. A command hook forwards each prompt and answer, and a
  launcher hook spawns a transcript tail that streams the full session over a
  WebSocket. Together they are what makes a terminal session a thread in Octomate.
- **MCP** gives the agent Octomate's tools: routing, history search, and the MCP
  connectors you installed. See [Octomate MCP](../../usage/mcp/octomate.md).

The server must have the matching agent tentacle enabled, because that is what
mounts `/hooks/<runtime>`.

## Configure the client first

```sh
uv tool install octomate-cli
octomate configure --url https://octomate.example.com --token '<api-token>'
```

Use the server's **base URL**. The MCP installer appends `/octomate/mcp` itself.
The token is one you [issued in Trunkline](../accounts.md#issue-a-client-token) with the
`hooks` and `mcp` scopes.

Each setting resolves independently, strongest first:

1. An explicit option on the command, where one exists.
2. `OCTOMATE_CLI_URL` or `OCTOMATE_CLI_TOKEN` in the environment. An empty value
   counts as unset.
3. `./.octomate/cli.toml` in the current directory. Write it with
   `configure --scope project`.
4. `~/.config/octomate/cli.toml`. The default `configure` target.

The client never reads a `.env` file: on a server machine that file is the
deployment's, and a server's file must not decide a person's credential. Both TOML
files are written with mode 600. Running `configure` again without `--token` keeps
the token that already resolves.

The project file exists so one directory can point at a different server, a staging
deployment for instance. Hooks run with the session's directory as their working
directory, so they find it. Switch files before starting a new conversation: a
running tail keeps the server it connected to, and switching mid-conversation splits
its hooks and its transcript across two servers.

Then install the hooks and the MCP entry for each runtime on the machine: [Claude
Code](claude-code.md), [Codex](codex.md) or [DeepSeek Harness](deepseek.md).

## What the tail does

The launcher hook fires on every prompt and spawns `octomate <runtime> tail`,
detached, so the turn never waits on it. The tail frames complete lines from the
transcript, ships them raw with their byte offsets, and lets the server assemble
turns. A transcript format change therefore never needs a client update.

- The server answers with where each file resumes, so a re-run never duplicates.
  `octomate claude tail --session <id> --path <transcript>` by hand backfills a
  session that ran while the server was down. Nothing sweeps for those on startup.
- One tail per session, guarded by a lock. A second launch during the previous
  turn's drain waits briefly, then yields.
- A tail exits when the server finalizes the turn, when the session has been quiet
  for 30 minutes, or when the server refuses it. A bad token or an old wire protocol
  is a refusal, printed once, with no retry.
- Subagent transcripts beside the session file stream under their own ids.

Hooks resolve the URL and token when they fire, so rotating a token means running
`configure` again and nothing else. Hooks do pin the absolute path of the interpreter
and `octomate` script that installed them, so reinstall after moving or reinstalling
the CLI environment. The transcript tail needs a POSIX host: on Windows, run the
agent and the CLI inside WSL.

## Verify

1. Send one distinctive prompt in a fresh native session. Its prompt and answer
   should appear as a thread in Trunkline. A prompt with no answer means the hooks
   arrived and the tail did not.
2. In the runtime's MCP UI, confirm the `octomate` server is connected. `mcp show`
   checks the file, not the connection.
3. Ask the agent to call `gateway_scry` with `reveal: "routes"`. It is read-only and
   returns the routes your account can reach.

## Rotate, move, or remove

- **New token or URL**: `octomate configure ...`, then re-run every `mcp install`.
  Hooks pick the change up on their own, unless you pinned `--url`.
- **Moved CLI environment**: re-run every `hooks install`.
- **Retire a client**: `octomate <runtime> hooks uninstall` and `mcp uninstall`
  with the scope you installed with, then revoke the token in Trunkline. Removing
  the local entries does not revoke anything.

## Common problems

| Symptom | Check |
|---|---|
| Hooks get 401 | Token validity, `hooks` scope, and which `cli.toml` this directory resolves |
| Hook path gets 404 | The agent tentacle is enabled on the server, and the proxy passes `/hooks/...` through |
| Prompts arrive, answers do not | The tail: WebSocket upgrade at `/hooks/<runtime>/stream`, idle timeouts on the proxy, transcript readable |
| MCP gets 401 | `mcp` scope and the embedded token; reinstall after rotation |
| MCP connected, tools missing in an open session | Restart the runtime; it reads the MCP file at launch |
| Works in a terminal, not from an editor | Absolute paths in the hook commands, hook trust in Codex, the editor's environment |
| DeepSeek tail exits at once | `DSH_LAUNCH_TOKEN`, and dsh restarted after the install |

A driven session started from a channel is different: it disables the runtime's own
hooks, plugins and MCP servers and gets Octomate's tools from the server directly.
Nothing on this page applies to it.
