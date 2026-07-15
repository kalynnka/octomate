# Plan: Claude native session ingest — live transcript tailer + recoverable external runs

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-07-14
> **Builds on:** the shipped native-ingest foundation — hook human-ledger
> ([claude/ingest.py](../../octomate/tentacles/agent/claude/ingest.py), commit `fc8b7de`),
> transcript schema ([claude/transcript.py](../../octomate/tentacles/agent/claude/transcript.py),
> commit `5cddad7`), and lazy restore
> ([claude/restore.py](../../octomate/tentacles/agent/claude/restore.py), commit `b72b039`).
> **Reuses:** [`ClaudeRunAccumulator`](../../octomate/tentacles/agent/claude/adapter.py#L151)
> (transcript-line → pydantic-ai messages + `StreamEvents`) and
> `ConversationManager.record_agent_run` ([conversation.py:100](../../octomate/managers/conversation.py#L100)).

## TL;DR

The shipped seam ingests a native Claude session two ways: hooks write the **live human
ledger** (clean prompt + final answer, per turn), and **restore reads the whole
transcript once** — at `SessionEnd` or on web-open — to rebuild the full model timeline
(thinking + usage). Restore is lazy and whole-file.

This plan adds the missing middle: **stream the model timeline live, incrementally, as
the transcript is written**, instead of only rebuilding it after the fact. A hook starts
a per-session **tailer** that follows the session's `.jsonl` from a byte offset, feeds
each new line through the same accumulator, and drives two sinks — a durable
`ExternalAgentRun` per completed turn (carrying its offset range) and a lossy live
`StreamEvents` stream for a future UI. Because every turn records the byte range it was
built from, ingest is **checkpointed and re-runnable**: a session interrupted by an
Octomate restart resumes from `max(end_offset)`, and a manual recovery re-tails any range
idempotently. Restore is not retired — it is refactored into the recovery engine.

**The invariant, unchanged from the foundation:** the transcript is a *complete* record,
so any offset range can be re-read to rebuild exactly the runs it contains. Live tailing
is a front-run of that same rebuild, not a second representation to reconcile.

## Why now — the gap between live-ledger and lazy-restore

| | Live human ledger (shipped) | Lazy restore (shipped) | **Live tailer (this plan)** |
|---|---|---|---|
| Source | hook events | whole transcript | transcript **delta** |
| Reads transcript? | never | once, whole file | follows from offset |
| Model timeline (thinking/usage) | ✗ | ✓ (after the fact) | **✓ (as it happens)** |
| Granularity | per turn | per session | per line → per turn |
| Feeds a live stream? | ✗ | ✗ | **✓ (lossy `StreamEvents`)** |
| Recoverable after a crash? | n/a | re-reads whole file | **from `max(end_offset)`** |

Restore already proved the hard part — `ClaudeRunAccumulator` consumes transcript lines
directly and rebuilds thinking + usage faithfully. What is missing is (a) an incremental
reader so the timeline is visible mid-run rather than at `SessionEnd`, and (b) durable
offsets so ingest survives interruption and can be re-driven from a UI.

## What already exists (grounding)

- **Hook pipe** — `POST /hooks/claude` → `ClaudeHookIngest.handle`
  ([ingest.py:52](../../octomate/tentacles/agent/claude/ingest.py#L52)) handles
  `UserPromptSubmit` / `Stop` / `SessionEnd`; `SessionEnd` already fires
  `restore_in_background`. Per-session `asyncio.Lock` serializes a session's writes.
- **Transcript schema** — `TranscriptLine` discriminated union + `transcript_line_adapter`
  ([transcript.py](../../octomate/tentacles/agent/claude/transcript.py)); `validate_json`
  accepts raw bytes.
- **Turn assembler** — `split_turns` + `record_turn`
  ([restore.py](../../octomate/tentacles/agent/claude/restore.py)) split lines into turns
  (a turn opens on a `prompt_source` user line; `is_sidechain` skipped), run each through
  a fresh `ClaudeRunAccumulator`, and `record_agent_run(run_id = prompt_id,
  external_id = session_id)` — **idempotent by `prompt_id`**.
- **Run model** — `AgentRun` ([schemas/runs.py](../../octomate/schemas/runs.py),
  [models/runs.py](../../octomate/models/runs.py)); PK `id` = pydantic-ai run_id, or
  `prompt_id` for a native turn. Thin: `conversation_id`, `name`, `started_at`, messages.
- **Router lifecycle** — the hook router is mounted by `Octomate.connect`
  ([base.py:139](../../octomate/tentacles/agent/claude/base.py#L139)); `session_restore`
  is a cached, shared singleton ([base.py:147](../../octomate/tentacles/agent/claude/base.py#L147)).

## Design

```
 native Claude session (CLI / VSCode / desktop)
   │
   ├─ hooks (http) ─▶ POST /hooks/claude ─▶ ClaudeHookIngest        (human ledger — unchanged)
   │                     ├─ SessionStart  → ensure thread+conversation (the "session skeleton")
   │                     │                  + ClaudeTranscriptTailer.start(session_id, transcript_path)
   │                     ├─ UserPromptSubmit → record_inbound(prompt)   ; start tailer if absent
   │                     ├─ Stop            → record_outbound(answer)
   │                     └─ SessionEnd      → tailer.finalize(session_id)  (final drain to EOF, then stop)
   │
   └─ ~/.claude/projects/<slug>/<session_id>.jsonl   (append-only, message-granular)
                    │
        ClaudeTranscriptTailer  (one task per session)
                    │  watchfiles.awatch(parent dir) ─▶ wake
                    │  read [offset .. EOF); split on '\n'; buffer trailing fragment
                    │  validate_json(complete line) ─┬─ ok  → feed line to turn assembler
                    │                                 └─ err → skip, still advance offset
                    │
             ┌──────┴───────────────────────────────────┐
     (a) live sink                             (b) durable sink
     per line: accumulator.consume(line)       at turn boundary (next prompt line, or EOF@finalize):
       → StreamEvents → object stream            record ExternalAgentRun(
       (bounded, DROP-on-full — never blocks)      run_id = prompt_id, messages,
                                                    session_id, source, start_offset, end_offset, last_uuid)
                                                  + bind human-ledger rows (by prompt_id)
                    │
        recovery(session_id)  ◀── manual trigger (web/app UI, later)
                    │  from = max(end_offset over committed external runs) or 0
                    └─ re-tail [from .. EOF) once → same assembler → idempotent by prompt_id
```

### The two sinks are decoupled on purpose

- **Durable** (`ExternalAgentRun`) is the source of truth and the checkpoint. A turn is
  committed **when its boundary is observed in the file** — the next `prompt_source`
  line, or EOF at `finalize`. Committing on the *file* boundary (not the `Stop` hook) is
  what removes the flush-lag settle-poll the old eager path needed: the turn's bytes are
  provably flushed once the line after it exists.
- **Live** (`StreamEvents`) is a forward-looking convenience for the UI channel. It is a
  bounded `anyio` memory stream with **drop-on-full** semantics, so a session nobody is
  watching never applies backpressure to the tailer. Durability never depends on a
  consumer existing.

## UoW-A — the offset tailer

A `ClaudeTranscriptTailer` owning `dict[session_id, task]`, one follow-loop per session.

1. **Follow loop.** Given `(session_id, transcript_path)` and a start `offset` (0 for a
   fresh session, `max(end_offset)` for a resume/recovery): `awatch` the parent dir;
   on any change to the target path, open `"rb"`, `seek(offset)`, read to EOF, append to
   a `bytes` buffer, `split(b"\n")` → complete lines + a trailing fragment kept for next
   wake. Advance `offset` past every complete line. `validate_json` each; skip (log)
   unknown/malformed lines **without holding the offset** (mirrors `read_lines`).
2. **Trailing fragment** is the only thing that pins the offset — never validate or emit
   it. No pydantic partial-JSON: newline framing is sufficient and avoids emitting
   half-built records.
3. **Truncation guard.** If `stat().st_size < offset`, reset `offset, buffer = 0, b""`
   (defensive; the file is append-only in practice).
4. **Turn assembly.** Reuse `split_turns`' rule incrementally: a `prompt_source` user
   line opens a turn and **closes the previous one** → commit the previous turn (UoW-B).
   Non-prompt user lines and assistant lines append to the open turn; `is_sidechain`
   skipped. `finalize` commits the last open turn.
5. **Live emission.** For each line, run `accumulator.consume(line)` and push the yielded
   `StreamEvents` to the session's object stream (non-blocking; drop on full).
6. **Startup wait.** If `transcript_path` does not yet exist, the dir-watch delivers its
   `Change.added`; bound the wait and let `finalize` drain regardless (covers sub-second
   one-shots where watch events coalesce).

**Acceptance:** running `claude` at the hook router streams `StreamEvents` for thinking /
tool calls / answer while the turn is in flight; killing and restarting Octomate
mid-session resumes with no duplicated or dropped runs; a malformed line does not stall
the cursor; a session with no stream consumer still records every run.

## UoW-B — polymorphic `ExternalAgentRun` (single-table)

Split the run into two typed variants on **one** table (single-table inheritance) — the
external columns ride on `agent_runs` as nullable, discriminated by `kind`, per the
data-modeling rule (distinct fields ⇒ distinct typed variants). No second table and no
join on the read path (chosen over joined-table precisely to keep run reads single-table).

- **ORM** ([models/runs.py](../../octomate/models/runs.py)): add a `kind` discriminator to
  `AgentRunModel` (`__mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity":
  "octomate"}`). `ExternalAgentRunModel(AgentRunModel)` — **no `__tablename__`**,
  `polymorphic_identity = "external"` — declares the extra columns, which SQLAlchemy
  places (nullable) on the shared `agent_runs` table:
  - `session_id: str | None` (indexed) — the native session / conversation key
  - `source: str | None` — the transcript `entrypoint` (`claude-vscode`, `claude-desktop`, `cli`)
  - `start_offset: int | None`, `end_offset: int | None` — the turn's byte range
  - `last_line_uuid: str | None` — last transcript-line `uuid` folded in (provenance / debug)
- **Transmuter** ([schemas/runs.py](../../octomate/schemas/runs.py)): `ExternalAgentRun(AgentRun)`
  blessing `ExternalAgentRunModel`, adding the four fields; base `AgentRun` stays
  `polymorphic_identity = "octomate"`.
- **Manager**: `record_external_run(...)` on `ConversationManager` writing the `external`
  variant with offsets — leaving the octomate `record_agent_run` path untouched
  (surgical). Restore's current `external_id=session_id` call folds into this.
- **Migration** (alembic): add the nullable columns + `kind` to `agent_runs` (SQLite via
  `batch_alter_table`); **backfill** `kind='external'` where `name = 'claude-native'` (the
  runs restore has already written), else `'octomate'`, and set `session_id` on the
  external rows from the owning conversation's `external_id`. No new table, no FK.

**Acceptance:** octomate-driven runs read back as `AgentRun` (`kind='octomate'`), restored
native runs as `ExternalAgentRun` with populated offsets; the existing restore/octomate
paths keep passing.

## UoW-C — lifecycle wiring

1. **Add `SessionStart`** to `HANDLED_HOOK_EVENTS`
   ([hooks.py:12](../../octomate/tentacles/agent/claude/hooks.py#L12)) and
   `ClaudeHookInput` (`source`, and `transcript_path` is already modeled). Handler:
   ensure thread + conversation (the skeleton) and `tailer.start(session_id,
   transcript_path)`. Make `UserPromptSubmit` **start-if-absent** so a missed
   `SessionStart` self-heals.
2. **`SessionEnd`** ([ingest.py:63](../../octomate/tentacles/agent/claude/ingest.py#L63)):
   `tailer.finalize(session_id)` — final drain to EOF, commit the last turn, close the
   live stream, drop the task and the per-session lock. **Remove the
   `restore_in_background` call**: `finalize`'s drain fully replaces it, so restore no
   longer runs automatically — it survives only as the manual recovery engine (UoW-D).
   Consequence: a session Octomate never watched live (down during the run, or hooks not
   wired) is rebuilt **on demand when the UI opens it**, not eagerly at `SessionEnd`.
3. **Ownership**: the tailer is a cached singleton on the tentacle beside `session_restore`
   ([base.py:147](../../octomate/tentacles/agent/claude/base.py#L147)); its tasks are
   cancelled on `disconnect`. Share the existing per-session `asyncio.Lock` so tailer
   run-writes and hook ledger-writes for one session serialize.

**Acceptance:** a full session start→turns→end produces the human ledger (hooks) and the
model timeline (tailer) with each turn's ledger rows bound to its run; tentacle shutdown
cancels every follow loop cleanly.

## UoW-D — manual recovery (UI-facing later)

Refactor restore into the recovery engine: extract the turn assembler so both the live
tailer and recovery call it. `recover(session_id)`:

1. Resolve the transcript (`locate_transcript` handles a moved slug).
2. `from = max(end_offset)` over the session's committed `ExternalAgentRun`s, else 0.
3. Re-tail `[from .. EOF)` once through the assembler; commit missing turns. Idempotent by
   `prompt_id` (already how `rebuild` dedups), so an overlapping re-drive is a safe no-op.

Exposed as a method now, wired to an endpoint when the web/app UI lands. **Manual only —
no automatic retry** (fail-fast; recovery is an explicit user action).

**Acceptance:** deleting the last N `ExternalAgentRun`s of a session and calling `recover`
reproduces exactly them, byte-for-byte identical offsets; calling `recover` on a complete
session is a no-op.

## Non-goals (explicit scope cuts)

- **Subagents / `is_sidechain`** — still skipped, as restore does today. Modeling them as
  child runs is future work.
- **Resume / compaction that forks to a new file** — treated as a fresh session
  (its own `session_id` + conversation) for v1; cross-session linking is later.
- **Cowork and web/cloud sessions** — no local transcript exists (sandbox / server-side),
  so the tailer cannot see them. OTEL is the only surface-spanning path; out of scope.
- **Token-delta streaming** — the transcript is message-granular (one line per API
  round-trip). Sub-message deltas exist only on the SDK path, not on disk.

## Risks

The durable sink is the safety net under most of these: every turn records the byte
range it was built from, so anything the live path drops is recoverable by re-tailing
from `max(end_offset)`, idempotent by `prompt_id`. Ordered by severity.

| Risk | Sev | Mitigation |
|---|---|---|
| **`SessionStart` is not delivered to `http` hooks** — verified on Claude Code 2.1.204: a `command` handler on `SessionStart` fires, an `http` one silently does not. If the tailer only started from `SessionStart` it would never start. | 🔴 | `UserPromptSubmit` is **start-if-absent** — the first prompt starts the tailer, so `SessionStart` is only a best-effort *earlier* start. `finalize` drains to EOF regardless, so a late start loses nothing. Re-verify per CLI version; treat `SessionStart` as optional. |
| **Unpaired model messages break history reuse** — an interrupted turn (user escapes then re-prompts, or death mid-tool-call) yields a `ModelResponse` whose `ToolCallPart` has no matching `ToolReturnPart`. Leaving the turn *incomplete* is fine, but pydantic-ai **rejects unpaired tool calls** when the history is fed to another agent (fork / teleport), which would break cross-agent reuse of native runs — the whole reason for the shared message frame. | 🔴 | The hook skeleton is the completeness oracle: commit a turn's run only once its `Stop` (the outbound the skeleton wrote) confirms it finished — a turn with an inbound but no outbound is still in flight, so its model messages are not recorded. Defensively, trim a trailing unpaired tool-call `ModelResponse` before `record_external_run`, mirroring `ConversationManager.drop_trailing_deferral`. A committed run is therefore always a valid, paired history. |
| **Octomate dies before `SessionEnd`/`finalize`**, leaving the last in-flight turn uncommitted (a turn commits only when the next prompt line appears, or at `finalize`). | 🔴 | Only the single trailing turn is exposed, and only until the session is next opened: recovery / next-open re-tails from `max(end_offset)`. Every turn before the last observed boundary is already durable, and an incomplete trailing turn is simply not committed (see the pairing row above). This is the whole point of committing on file boundaries + offsets. |
| **Migration backfill of `kind` + `session_id`** on the live `agent_runs` table — `session_id` lives on the conversation's `external_id` today, not on the run, so it must be derived; a stray octomate run named `claude-native` would be miscategorized as external. | 🔴 | Add the nullable columns + `kind` via `batch_alter_table` (SQLite); backfill `kind='external'` only where `name='claude-native'` (what restore writes), derive `session_id` from the owning conversation, and **dry-run the migration against a copy of the real DB** before applying (as the `conversation.status` migration was). Single-table ⇒ no new table or FK to create. |
| **Tailer and recovery must compute `start/end_offset` identically** — restore tracks no offsets today; if the UoW-D refactor and the live tailer disagree on a turn's byte range, `max(end_offset)` resumes at the wrong place and can skip or double a turn. | 🟡 | One assembler owns offset accounting; pin offsets only past complete, newline-framed lines; never assume ranges are contiguous (a skipped bad line leaves a gap — `max(end_offset)` is still the correct resume point). `prompt_id` idempotency absorbs any overlap on re-drive. |
| **Newline framing across read boundaries** — a line, or a multi-byte UTF-8 char, split across two `awatch` wakes. | 🟡 | Frame on raw `b"\n"` over `bytes` (never decoded text); buffer the trailing fragment and advance the offset only past complete lines; never `validate_json` the fragment. |
| **Turn committed before its last line is flushed.** | 🟡 | Commit on the **file** boundary (next `prompt_source` line, or EOF at `finalize`), never on the `Stop` hook — the turn's bytes are provably on disk once the line after it exists. This retires the old flush-lag settle-poll. |
| **Shared per-session lock couples tailer and hooks** — a long follow-loop drain holding the lock would block `UserPromptSubmit`/`Stop` ledger writes. | 🟡 | Take the lock only around individual run/ledger commits; the follow loop reads, frames, and assembles *outside* the lock and holds it just long enough to write a completed turn. |
| **`awatch` misses a change** — coalesced sub-second events, network / virtual FS, editor write patterns. | 🟡 | Offset reads are idempotent and `finalize` always drains to EOF; `watchfiles` `force_polling` covers FS types that don't emit events. |
| **Abandoned session leaks a follow task + watcher** — a session that never sends `SessionEnd` (crash, hooks removed) keeps its loop alive; same unbounded-map caveat as the existing `locks` / `states` dicts. | 🟡 | Cap concurrent tailers and idle-timeout a loop that sees no new bytes; `disconnect` cancels every loop. |
| **A drifted / unmodeled transcript line.** | 🟡 | Skip + log but **advance the offset past it** — a bad line never wedges the cursor. `extra="allow"` tolerates new *fields*; only a changed *shape* is skipped. |
| **Single-table sparsity** — the external columns sit NULL on every octomate run (the STI tradeoff taken over a join). | 🟢 | Five nullable columns on one table; reads stay single-table, so the octomate hot path (`conversation.messages`, joined through `agent_runs`) is untouched — which is why STI was chosen. Revisit only if the external variant grows many more fields. |
| **`watchfiles` is a new (Rust) dependency** with FS-specific behavior (FSEvents on macOS, inotify on Linux). | 🟢 | Well-maintained and widely used; `force_polling` covers exotic FSes; correctness rests on offsets, not on event fidelity. |
| **In-place compaction rewriting the transcript** would shift every offset and invalidate checkpoints. | 🟢 | Out of scope (resume-to-new-file is a non-goal); the truncation guard resets on shrink and `prompt_id` idempotency re-commits, so the worst case is a one-time re-tail, never corruption. |
| **Live `StreamEvents` drop-on-full** loses events for a slow/absent consumer. | 🟢 | By design — the UI treats the live stream as display-only and hydrates completeness from the durable `ExternalAgentRun`s; durability never depends on a consumer existing. |
| **Many concurrent sessions ⇒ many watch threads.** | 🟢 | Fine at human concurrency; collapse to one parent-dir watcher with per-session demux if it grows. |
| **Schema drift in `usage` or a new line kind.** | 🟢 | `extra="allow"` + skip-unknown keeps ingest live; a genuinely new `usage` shape degrades to zero counts, never a crash. |
| **Same-host assumption** — the tailer follows a local file. | 🟢 | Cloud / cowork sessions have no local transcript (a non-goal); a remote Octomate would need the bytes shipped, which the hooks already assume by carrying a local `transcript_path`. |
