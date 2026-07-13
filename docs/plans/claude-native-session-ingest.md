# Plan: Claude native session ingest — live human ledger + lazy transcript canonicalization

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-07-13
> **Supersedes:** the inbound half of the earlier "bi-directional visibility" sketch
> (the outbound `CLAUDE_CODE_ENTRYPOINT` tag already shipped —
> [base.py:371](../../octomate/tentacles/agent/claude/base.py#L371), commit `703ae9b`).
> **Builds on:** the committed transcript schema
> ([claude/transcript.py](../../octomate/tentacles/agent/claude/transcript.py), commit
> `5cddad7`) and the run adapter
> ([claude/adapter.py](../../octomate/tentacles/agent/claude/adapter.py)).

## TL;DR

A native Claude session (started in the app / CLI / VSCode, **not** driven by our
`claude` tentacle) should become durable Octomate history in the
`thread → conversation → run → model-message` frame. One session has two sources of
truth that cost very differently, so ingest splits along that seam:

1. **Live human ledger (eager, from events, never touches the transcript).** As hooks
   fire, write only the prompt/answer chat log — `UserPromptSubmit` carries the clean
   prompt, `Stop` carries the final answer, both **complete and lossless**. That is the
   "in-the-air pipe": a session is visible the moment it runs. **No model messages
   live** — the event stream can't express them completely (no thinking, no usage, no
   tool-interleaving), so nothing partial is persisted.
2. **Full model timeline (lazy, from the transcript, on restore).** When a surface
   opens the conversation — the UI channel we are going to build — read the local
   transcript and materialize that session's runs + model messages at full fidelity,
   then wire the human-ledger rows to them. The VSCode-plugin-on-open behavior, in our
   model-message representation.

**The invariant that makes this clean:** the transcript is a *complete* record, so
**restore is self-sufficient** — it can build the human ledger *and* the model timeline
from the transcript alone. Live is therefore not a second representation to reconcile;
it is a pure optimization that front-runs the human ledger for in-flight visibility. If
you never open a session you keep the live ledger; when you open it, restore fills in
everything; if Octomate was down during the session, restore still builds it all.

## Why re-plan — the defect in the eager approach

The previous direction read the **whole transcript at every `Stop`** and rebuilt every
turn's full model-message set (thinking + usage) through the adapter. Two costs, both
avoidable, plus a bonus:

| Cost | Eager-at-`Stop` | This plan |
|---|---|---|
| Transcript I/O during a live run | re-parses the whole file every `Stop` | **never reads it live** — events only |
| Rebuilding thinking/usage | every turn, whether or not anyone looks | **only on restore**, once, on demand |
| Transcript flush lag | `Stop` fires ~100 ms before the file flushes, forcing a settle-timeout poll | **gone** — live uses events; restore reads a long-settled file |
| Persisting lossy model messages | event-derived messages missing thinking/usage | **never** — model messages come only from the complete transcript |

The flush-lag elimination is the tell the seam is right: the poll only existed because
the eager path read a file that the event triggering it had not finished writing.

## What already exists (grounding)

- **Transcript schema** — [transcript.py](../../octomate/tentacles/agent/claude/transcript.py)
  is a typed discriminated union (`TranscriptLine`) over every on-disk line kind, plus
  `transcript_lines_adapter`. Restore parses through this.
- **Run adapter** — [`ClaudeRunAccumulator`](../../octomate/tentacles/agent/claude/adapter.py#L151)
  maps an SDK message stream → pydantic-ai `ModelMessage`s (thinking, usage, signatures,
  native tools handled). The outbound tentacle uses it; restore reuses it.
- **Whole-turn persistence** — `ConversationManager.record_agent_run`
  ([conversation.py:100](../../octomate/managers/conversation.py#L100)) writes a run and
  its messages in one commit; the `ThreadManager` set (`record_inbound`,
  `record_outbound`, `bind_messages`, `bind_assistant_replies` —
  [thread.py](../../octomate/managers/thread.py)) writes and cross-references the human
  ledger. Restore reuses `record_agent_run` **verbatim** — it builds a whole turn at
  once, so no incremental/append surface is needed.
- **Outbound visibility** — shipped; not in scope here.

## Design

```
 native Claude session
   │
   ├─ hooks (http) ─▶ POST /hooks/claude ─▶ ClaudeHookIngest   (in-memory; NO transcript I/O)
   │                                          ├─ UserPromptSubmit → ensure thread(claude-native, session_id)
   │                                          │                     + record_inbound(prompt), tag = prompt_id
   │                                          ├─ Stop             → record_outbound(last_assistant_message), tag = prompt_id
   │                                          └─ SessionEnd       → (optional) close thread
   │                                          # human ledger only — complete + lossless. no runs, no model messages, no bindings.
   │
   └─ ~/.claude/projects/<slug>/<session_id>.jsonl              (full fidelity, on disk)
                    │
        restore(session_id)  ◀── UI channel opens the thread   (lazy, on demand)
                    │
                    ├─ transcript_lines_adapter.validate → TranscriptLine[]
                    ├─ split into turns (prompt_source marks a real prompt; prompt_id = run id)
                    ├─ per turn NOT already a run (idempotent by run_id = prompt_id):
                    │     ├─ rebuild messages at full fidelity via the adapter (thinking + usage)
                    │     ├─ record_agent_run(run_id = prompt_id, messages, external_id = session_id)
                    │     └─ bind the human-ledger rows (matched by prompt_id):
                    │           inbound  → request_source(user ModelRequest)
                    │           outbound → assistant_reply(ModelResponse)
                    └─ (self-sufficient: creates any human-ledger row live never wrote)
```

### UoW-A — Live human ledger from hook events

The in-the-air pipe. No transcript read, no model messages.

1. **Hook settings + router.** A typed `claude_hook_settings(url)` fragment (native
   `{"type":"http"}` handlers) for the handled events; `POST /hooks/claude` mirroring
   [web/vercel/routes.py](../../octomate/tentacles/channel/web/vercel/routes.py);
   registered in `main.create_app`, inert until a client points at it. Handled live:
   `UserPromptSubmit`, `Stop`, `SessionEnd` (the human-ledger events; tool/message-display
   events are model-timeline detail and are ignored live — they come from the transcript
   on restore).
2. **`ClaudeHookIngest`** ensures a `claude-native` thread (`chat_id = session_id`) and:
   - `UserPromptSubmit` → `record_inbound` the clean `prompt` (the transcript pads the
     prompt with injected `<system-reminder>` / `<ide_opened_file>` blocks; the event is
     the only clean copy), stamped `platform_message_id = prompt_id`.
   - `Stop` → `record_outbound` the `last_assistant_message`, stamped
     `platform_message_id = prompt_id`.
   - `SessionEnd` → optionally mark the thread closed.
   - No conversation, no runs, no model messages, no bindings, no transcript read.

   **Acceptance:** driving `claude -p` at the router produces, per turn, one inbound +
   one outbound `ThreadMessage` (a complete human chat log) with **zero transcript
   reads**; each is tagged with its `prompt_id`; re-firing an event is idempotent; a
   session that crashes before `Stop` leaves a clean inbound-only turn.

### UoW-B — Full model timeline from the transcript (restore)

Full fidelity, on demand, self-sufficient.

1. **`hydrate(session_id)`** ensures the conversation, parses the transcript via
   `transcript_lines_adapter`, splits into turns (`prompt_source` marks a genuine prompt;
   tool-result user lines carry `prompt_id` but no `prompt_source`; `is_sidechain` lines
   skipped), and for each turn whose `run_id (= prompt_id)` is not already a run:
   - rebuild the turn's messages at full fidelity (thinking + usage + signatures),
   - `record_agent_run(run_id = prompt_id, messages, name="claude-native", external_id = session_id)`,
   - **bind** the human-ledger rows matched by `prompt_id`: inbound → `request_source`
     on the user `ModelRequest`; outbound → `assistant_reply`. If a row is missing
     (live never ran, or a crash), create it from the transcript so restore stands alone.
2. **Reuse the adapter** for the rebuild (Decision 2), timestamps from each transcript
   line's own clock so `started_at`/ordering are historical.
3. **Idempotent** by `run_id`; re-hydrate builds only turns added since (a resumed
   session appends turns). No message is ever rewritten in place.

   **Acceptance:** after `hydrate`, each run's messages include thinking blocks with
   signatures and per-response usage matching the transcript, identical to what an
   outbound tentacle run of the same session would persist; the live human-ledger rows
   are now bound to their runs; `hydrate` on an unchanged transcript adds nothing;
   `hydrate` with no prior live ledger builds both ledgers from the transcript alone.

### UoW-C — Restore trigger for the UI channel

Wire the on-open hydration the design is built around.

1. **Locate the transcript by `session_id`** — glob `~/.claude/projects/*/<session_id>.jsonl`
   (no schema churn; same-host assumption, which the local events already make).
2. **A restore entry point** the future UI channel calls when it opens a thread; for now
   a thin manual trigger (`POST /threads/{id}/hydrate` or a function) so UoW-B is
   drivable before the UI exists.
3. **Hydrate-on-open** — a thread stays human-ledger-only until first opened; opening
   hydrates it (idempotent). Optionally cache a "hydrated-through" cursor to skip
   re-reading an unchanged transcript on every open (Decision 4).

   **Acceptance:** opening an un-hydrated thread builds its model timeline and returns
   it; a re-open with no new turns re-reads nothing (with the cursor) or is a cheap
   no-op (without).

## Decisions

1. **✅ Resolved — live persists the human ledger only.** The event stream can't express
   model messages completely (no thinking/usage), so nothing partial is persisted live;
   the full model timeline is rebuilt from the transcript on restore. (This is what
   collapses the plan: `record_agent_run` is reused verbatim, no append surface, no
   provisional run state, no live/transcript reconciliation.)
2. **Restore's rebuild path.** Recommend **reuse the adapter** by feeding the transcript's
   raw lines through `parse_message` → `ClaudeRunAccumulator` (empirically round-trips
   real transcripts; one translator shared with the outbound tentacle), using
   `TranscriptSchema` for the envelope, turn-splitting, and non-message lines
   (`ai-title` → conversation name, `pr-link`, …). *Alternative:* a typed converter
   straight from `TranscriptAssistantLine.message` (an `anthropic` `Message`) — DRYs onto
   the new schema but forks the block-mapping. Settle whether the SDK-private
   `parse_message` coupling (already relied on in the tentacle) is acceptable here.
3. **Human-ledger ↔ model-message correlation.** Recommend matching by **`prompt_id`**
   stamped on `ThreadMessage.platform_message_id` at ingest, so restore can find "the
   inbound/outbound rows for turn `p1`" and bind them to run `p1`. Confirm nothing else
   keys on `platform_message_id` for `claude-native` threads.
4. **Re-hydration cost.** Recommend **idempotent-by-`run_id`** (build only new turns) +
   an optional conversation-level "hydrated-through prompt_id" cursor so a re-open of an
   unchanged transcript reads nothing. *Alternative:* re-read every open (simpler, but
   O(transcript) per open). No new run column either way.
5. **`SessionEnd` handling.** Recommend a light **close the thread** (needs a
   `ThreadManager` status setter — `Thread` has a `status` field but no setter yet).
   Optional for v1; nothing depends on it since each turn is finalized at its own `Stop`.

## Risks

- 🟡 **Correlation robustness.** Restore binds live rows by `prompt_id`. A crash between
  `UserPromptSubmit` and `Stop` leaves an inbound with no outbound; restore must create
  the outbound from the transcript rather than assume the pair exists. Covered by the
  "self-sufficient" acceptance in UoW-B.
- 🟡 **`Stop.last_assistant_message` fidelity.** It is the final turn text; a turn whose
  last act is a tool call (no closing prose) yields an empty/asymmetric answer. Live
  outbound may then be empty; restore's transcript rebuild is authoritative regardless.
- 🟡 **Transcript location / same-host.** Restore globs the local projects dir; a remote
  Octomate would need the bytes shipped — the same assumption the local events already
  make. Out of scope to solve now.
- 🟡 **`run_id = prompt_id` stability.** The idempotency story rests on it. Verified for
  live sessions; re-confirm against a `--resume`d session (reused `session_id`, fresh
  `prompt_id`s → new turns, no duplicate runs).

## Verification

- Unit (`tests/agent/test_claude_hook_ingest.py`): **live** = human ledger only from a
  scripted event sequence (inbound+outbound tagged with `prompt_id`, idempotent re-fire,
  crash-before-`Stop` leaves a clean inbound-only turn, **zero transcript reads** —
  spy/patch the reader); **restore** = from a fixture transcript (runs + model messages
  with thinking/usage, bindings onto the live rows, idempotent re-hydrate, and a
  transcript-only hydrate with no prior live ledger).
- End-to-end: drive a real `claude -p` session into a booted router on a scratch DB →
  assert the live human ledger and that the transcript was never read during the run →
  `hydrate` → assert the full timeline + bindings → `--resume` a second turn → assert no
  duplicate run and the new turn appears on the next hydrate.
- `uv run pytest -q` green; `ruff` clean; `pyright` no new errors (baseline 14).
- Land per UoW (A → B → C) as separate commits.

## Non-goals

- The **outbound** tentacle keeps its in-process SDK persistence (richest source).
- **Web-timeline rendering** — a UI channel's history read/render path is its own work;
  this plan makes the rows exist and be hydratable.
- A separate **hook-event table / change-feed** — reaffirmed dropped; the frame is the
  record. Live per-tool-call visibility for a notification center, if wanted later, is a
  change-feed concern, not this plan.
- **Sidechains** (sub-agent runs) — skipped in v1.
