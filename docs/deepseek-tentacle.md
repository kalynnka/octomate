# DeepseekTentacle — WIP

The `deepseek` agent tentacle drives DeepSeek Harness the way dsh's own web client
does: it speaks the `/api` gateway — HTTP for unary calls, the mux WebSocket
at `/api/remote.mux` for session streams and approval/question events, and
`POST /api/$events/result` for answers. It attaches to a
dsh already serving the configured `host:port` and starts a `dsh web` child on
that same fixed port only when nothing answers, because dsh has no
cross-process lock on session logs and two harnesses over one `$DSH_HOME`
corrupt them; only a child of its own is stopped on shutdown. These are the
limitations the first version accepts, and why.

## Accepted limitations

1. **Native ingest reads the gateway through a client-side tail, and is
   turn-grained.** Native dsh sessions are ingested the way Claude's and
   Codex's are — hooks plus a per-session stream — with one substitution:
   `octomate deepseek tail` reads no file. dsh's log is zstd-framed and only
   advances at checkpoints, so the tail polls *its* machine's dsh gateway
   (`session/follow` for a snapshot, then `session/page` for earlier records
   at that snapshot's fixed cursor) and ships each event to `/hooks/deepseek/stream`, the
   event's dense `seq` standing where byte offsets stand. The
   `dsh-hooks-claude-code` bridge POSTs `UserPromptSubmit`/`Stop` into
   `/hooks/deepseek` for the skeleton and the turn boundary, and the launcher
   hook on the prompt event spawns the tail. The octomate server never speaks
   to a client machine's dsh. The consequences accepted with that:
   - A turn appears in octomate when it closes, not token-by-token; the
     prompt/answer ledger rows land at turn close too, because dsh's hook
     dialect carries no per-turn key and no answer to sketch them from live.
   - Sessions run while octomate was down are caught up when their next
     prompt's launcher spawns a tail (the server re-welcomes it at the
     committed floor); a session never touched again stays unrecorded — there
     is no startup sweep.
   - For a session whose last turn is still open, the gateway's reader
     *synthesizes* `turn/end {reason: interrupted}` closers in memory, and the
     session's own process may later write different events at those seqs. The
     tail therefore withholds a trailing interrupted turn until a successor
     event proves the closers durable — so an abandoned crashed session keeps
     its last turn unrecorded until the session is touched again.
   - The tail polls the gateway (~1s) and reads the dsh default endpoint
     (`http://127.0.0.1:3080`; `$DSH_API_URL` or `--dsh-url` override a moved
     port). Supply `DSH_LAUNCH_TOKEN` in the tail process's environment, or use
     the complete launch URL as `DSH_API_URL`. For hooks that spawn tails, the
     hook process must inherit the token too; setting it in a different shell
     after dsh starts does not change that process's environment. A manual
     `octomate deepseek tail` can backfill using the current launch token.
   - dsh subagent child sessions (`origin: subagent`) are classified by the
     tail against `session/list` and skipped rather than ingested as threads
     of their own — the server never sees the session header, so a hand-run
     tail on a child id would mis-file it.
   - A turn's prompt arrives *inside* it (dsh opens the turn, then splices the
     inbox into the step), and dsh logs injected user-role messages there too.
     The ledger row joins every `source.kind == "user"` message of the turn —
     steered prompts included — and leaves the harness's own injections
     (`agent-instructions`, `plugin` context) to the replay metadata.
   - All hook and stream traffic is ingested as external sessions, including
     sessions started by Octomate. Their native threads remain separate from
     the SDK conversations, as with Claude and Codex.
   - The hooks bridge mounts via `$DSH_HOME/cordis.patch.yml`, which every dsh
     process sharing that home loads — but the bridge package ships outside
     dsh's bundled dependency closure, so the first install must link it
     (`--bridge <checkout>/packages/hooks/hooks-claude-code`), and dsh
     processes need a restart to pick the row up.

2. **The permission vocabulary is the two shipped presets.**
   `DeepseekPermissionMode` is `workspace-write | danger-full-access` — what dsh
   ships by default. dsh's preset table is deployment-configurable, so a
   deployment composing a custom preset (say a `read-only` one) cannot be
   expressed without widening the literal in `octomate/types/permissions.py`.
   There is no permission RPC upstream; the tentacle switches presets with the
   `/permission <preset>` command on the Remote API (`commands/execute`), and
   a deployment that removed the permission-preset plugin fails the run rather
   than running under an unknown posture.

3. **Model and effort selection is durable session state, not per-turn.**
   dsh has no per-turn model override, so the tentacle calls
   `session/selectModel` before each prompt to make the octomate route win. A
   human driving the same session from another dsh client mid-conversation
   races that write. The effort map (`minimal/low → off`, `medium/high → high`,
   `xhigh → max`) is the `llm-deepseek` adapter's vocabulary; a deployment
   routing another adapter overrides `agents.deepseek.efforts`.

4. **A session's cwd is fixed at creation.** `session/create` takes the
   thread's project root (or `agents.deepseek.cwd`), and dsh offers no way to move
   an existing session. A thread that joins a project *after* its first dsh run
   keeps the old session cwd for that conversation.

5. **Text-only prompts.** dsh's `PromptContentPart` supports images, but v1
   flattens the prompt to text. Run-level `instructions` (a subagent spawner's
   framing) are prepended to the prompt text — dsh has no separate
   instructions channel.

6. **No structured output.** `session/prompt` has no output-schema knob, so
   `output_type` is refused with a `ValueError`.

7. **No mux reconnect.** The event socket dropping fails in-flight runs fast
   (after persisting what accumulated) instead of resuming — a deliberate
   departure from the VS Code extension's backoff loop. A started child on
   loopback dropping its socket is a broken harness, not weather; an attached
   harness going away (the operator stopped their `dsh web`) fails the same
   way. Neither is restarted or re-attached mid-flight; re-entering the
   tentacle (restarting octomate) is the recovery.

8. **A hard-killed octomate orphans a `dsh web` child it started.**
   `__aexit__` tearing down sends SIGTERM (escalating to SIGKILL) to a child
   of its own — an attached harness is deliberately never stopped — but dsh
   has no parent-liveness flag, so nothing protects against octomate itself
   being SIGKILLed. The orphan holds ~40–140 MB until killed by hand, though
   a later octomate boot will attach to it rather than double up.

9. **Non-interactive runs auto-reject approvals.** A commissioned (subagent)
   run has no human to ask, and dsh's ask-vs-never policy lives inside the
   preset rather than in a swappable posture, so the bridge declines approvals
   at once and cancels questions. A non-interactive run that needs tool
   escalation should run under `danger-full-access`.

10. **Question cards flatten dsh's answer shape.** dsh questions allow
    multi-select plus custom text per item; an octomate question card returns
    one text answer per question. The bridge maps an answer matching an option
    label to `selected` (labels echoed pristine, as dsh matches by label) and
    anything else to `custom` — so a multi-select can only ever carry one
    selection from octomate.

11. **Version checks are advisory; protocol checks are required.** The adapter
    is tested with dsh `0.1.6-alpha.1` (checkout `0d1f50007f`). Before spawning,
    Octomate reads `dsh --version`, logs an exact match, and warns for a different
    or unrecognized version. Development checkouts can change without a version
    bump, so authentication, `settings/describe`, `session/modelCatalog`, and the
    Remote event handshake also have to succeed. Existing servers do not publish
    their version: attachment logs that limitation rather than presenting the
    local executable's version as the running server's version.

12. **Octomate connects over loopback with authentication.** A started
    harness's launch token is exchanged automatically for a cookie used by HTTP
    and WebSocket requests. To attach to an existing harness, set
    `tentacles.deepseek.launch_token` from its launch URL. This is a local dsh
    credential, separate from the model provider's API key. Captured diagnostics
    redact launch tokens. An authentication or protocol error fails startup
    without launching another harness on that endpoint.

    For human browser access, Octomate prints `dsh web: <url>?token=...` once to
    the console when the child reports readiness, following dsh's launch banner.
    It writes no login-link file. The token stays in memory for Octomate's cookie
    exchange. The launch banner bypasses logging integrations; later dsh
    diagnostics redact the token.
    An attached, independently launched harness keeps its own launch-link workflow.

13. **Subprocess diagnostics are bounded.** Normal dsh output goes to the
    `octomate.tentacles.deepseek.process` logger at DEBUG. Startup failures include
    the first four and last 24 diagnostic lines, clipped to 500 characters each,
    with an omitted-line count. Runtime stderr raises one warning per process;
    subsequent detail remains at DEBUG. The console renders Python exceptions
    with Rich, at most 12 stack frames, and no local-variable dump. Enable DEBUG
    for that logger when the complete dsh output is needed.

## Browser access

For browser access through a reverse proxy, set `browser_url` to its origin:

```yaml
tentacles:
  deepseek:
    browser_url: https://dsh.example:8443
```

Octomate passes this authority as `--trusted-host` when starting dsh and uses it
in the printed login link. The proxy must serve the root `/` and preserve the
browser's Host and Origin. Octomate continues connecting to dsh over loopback.

After dsh starts, open the complete link printed in the console.
Its `?token=...` is a dsh browser credential,
separate from a provider API key or an Octomate API token. dsh exchanges it for
a browser cookie and redirects to `/` without the token. The credential grants
access to the harness rather than an individual account. A fresh dsh process
creates a new token; existing cookies can remain valid until their expiry
(30 days by default).

## Isolated integration test

Build dsh first, then run from the Octomate checkout:

```sh
DSH_TEST_EXECUTABLE=/absolute/path/to/deepseek-harness/apps/cli/lib/bin.js \
  uv run pytest tests/agent/test_deepseek_live.py -q
```

The opt-in test uses temporary dsh settings, sessions and workspaces, in-memory
Octomate managers, and a keyless mock model. It checks authentication, discovery,
streaming, tools, resumed sessions, approvals, questions, cancellation and native
history reads. It does not use a real model API or the operator's database.

## Verifying against a real dsh (manual smoke)

To check your own channel and model credentials after the isolated test:

1. Either run `dsh web` yourself (octomate attaches to it at
   `127.0.0.1:3080`), or let octomate start one: put `dsh` on `PATH`, or set
   `tentacles.deepseek.executable` to `<checkout>/apps/cli/lib/bin.js`. When
   attaching, configure `launch_token` from the existing harness's launch URL.
2. In the config home's `tentacles.yaml`, enable a `deepseek` tentacle with
   `type: deepseek` and list its id under the channel's `agents:`. The harness supplies
   the model/provider catalog and its default; no model list is needed.
3. Boot Octomate and check the dsh version/attachment message and successful
   Remote API probe. `extra_args` such as `--patch` are placed before the web
   app's `--host`, `--port`, and `--no-open` flags.
4. Summon dsh from the channel: a turn should stream text (and thinking) live,
   and the run should appear in thread history with the dsh session id as the
   conversation's `external_id`.
5. Under `workspace-write`, ask for something that escalates (e.g. writing
   outside the workspace): an approval card should appear, and both approve
   and reject paths should unblock the turn.
6. Switch the conversation's permission mode to `danger-full-access` and
   confirm the next run sends `/permission danger-full-access` (tentacle log)
   and no longer asks.
7. For native ingest: `octomate deepseek hooks install --bridge
   <checkout>/packages/hooks/hooks-claude-code`, restart the dsh web daemon,
   ensure the tail process receives the current dsh launch token, then drive a
   session in dsh's own web UI. The tentacle log should show the
   `deepseek.hook` lines as you prompt and a `remote tail connected` line as
   the launcher's `octomate deepseek tail` attaches; the turn should appear as
   a `deepseek-native` thread (prompt + answer rows, full model timeline) once
   it completes, and the tail process should exit shortly after the `Stop`
   settles. Prompting the same session again should not duplicate runs.
