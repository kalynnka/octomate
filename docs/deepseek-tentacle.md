# DeepseekTentacle — v1 limitations

The `deepseek` agent tentacle drives DeepSeek Harness the way dsh's own web client
does: it speaks the `/api` gateway — HTTP for unary calls, the mux WebSocket
for events, `POST /api/respond` for approvals and questions. It attaches to a
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
   (`session.history`, which serves the log decoded and unpacked, cold
   sessions included) and ships each event to `/hooks/deepseek/stream`, the
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
     port).
   - dsh subagent child sessions (`origin: subagent`) are classified by the
     tail against `session.list` and skipped rather than ingested as threads
     of their own — the server never sees the session header, so a hand-run
     tail on a child id would mis-file it.
   - A turn's prompt arrives *inside* it (dsh opens the turn, then splices the
     inbox into the step), and dsh logs injected user-role messages there too.
     The ledger row joins every `source.kind == "user"` message of the turn —
     steered prompts included — and leaves the harness's own injections
     (`agent-instructions`, `plugin` context) to the replay metadata.
   - Suppression of octomate's own driven sessions is a live claim around each
     driven turn: their hooks are dropped and their tails refused at the
     stream handshake. A driven session later prompted *natively* (dsh's web
     UI on the same session, or after an octomate restart) ingests as a native
     thread and re-records its turns there — the same exposure the Claude and
     Codex ingests accept.
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
   `/permission <preset>` command on the undocumented typert remotes plane, and
   a deployment that removed the permission-preset plugin fails the run rather
   than running under an unknown posture.

3. **Model and effort selection is durable session state, not per-turn.**
   dsh has no per-turn model override, so the tentacle calls
   `session.selectModel` before each prompt to make the octomate route win. A
   human driving the same session from another dsh client mid-conversation
   races that write. The effort map (`minimal/low → off`, `medium/high → high`,
   `xhigh → max`) is the `llm-deepseek` adapter's vocabulary; a deployment
   routing another adapter overrides `agents.deepseek.efforts`.

4. **A session's cwd is fixed at creation.** `session.create` takes the
   thread's project root (or `agents.deepseek.cwd`), and dsh offers no way to move
   an existing session. A thread that joins a project *after* its first dsh run
   keeps the old session cwd for that conversation.

5. **Text-only prompts.** dsh's `PromptContentPart` supports images, but v1
   flattens the prompt to text. Run-level `instructions` (a subagent spawner's
   framing) are prepended to the prompt text — dsh has no separate
   instructions channel.

6. **No structured output.** `session.prompt` has no output-schema knob, so
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

11. **An old dsh still opens a browser tab.** A started child is passed
    `--no-open`, but `dsh web` parses without `allowUnknownOption`, so a dsh
    predating the flag refuses it and exits instead of ignoring it. There is no
    version probe: the flag is offered, and a refusal naming it costs one
    failed spawn before the child is started again without it — which then
    opens a tab on the octomate host. A refusal naming any other flag (one from
    `extra_args`) fails the start instead of being retried.

12. **Whatever answers `host:port` is trusted.** dsh's `/api` has no TLS and
    no auth, so the attach probe (`host.describe`) trusts anything that
    answers it. `agents.deepseek.host` is therefore validated to loopback at
    config load — the trust fence is the machine — and a remote dsh (the VS
    Code extension's attach-but-never-start case) is out of scope for v1.

## Verifying against a real dsh (manual smoke)

Not covered by CI — the unit tests fake the gateway. To smoke-test live:

1. Either run `dsh web` yourself (octomate attaches to it at
   `127.0.0.1:3080`), or let octomate start one: put `dsh` on `PATH`, or set
   `agents.deepseek.executable` to a built dsh (for a monorepo checkout:
   `node <checkout>/apps/cli/lib/bin.js`, via a wrapper script).
2. In the config home's `agents.yaml`, add an `agents.deepseek:` block with a claim
   for `deepseek-v4-pro`, and list `- agent: deepseek / model: deepseek-v4-pro` under
   a channel's `agents:` in `channels.yaml`.
3. Boot octomate and check the tentacle log for the `attached to the dsh
   serving http://127.0.0.1:3080/` line — or, when nothing was running, the
   `started dsh at http://127.0.0.1:3080/` warning with its shared-`DSH_HOME`
   caution.
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
   then drive a session in dsh's own web UI. The tentacle log should show the
   `deepseek.hook` lines as you prompt and a `remote tail connected` line as
   the launcher's `octomate deepseek tail` attaches; the turn should appear as
   a `deepseek-native` thread (prompt + answer rows, full model timeline) once
   it completes, and the tail process should exit shortly after the `Stop`
   settles. Prompting the same session again should not duplicate runs.
