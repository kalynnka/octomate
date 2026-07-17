# Native session ingest — the Claude design, as a template

> **Status:** shipped (Claude) · proposed (Codex) · **Owner:** @luhui · **Created:** 2026-07-16
> **Reference implementation:** [claude/ingest.py](../../octomate/tentacles/agent/claude/ingest.py),
> [claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py),
> [claude/hooks.py](../../octomate/tentacles/agent/claude/hooks.py),
> [claude/transcript.py](../../octomate/tentacles/agent/claude/transcript.py)

## TL;DR

A **native session** is one a human runs in the vendor's own client — Claude Code in a
terminal or IDE — with no Octomate in the loop. Native ingest records that session into
Octomate's model (thread → conversation → run → messages) as it happens, so work done
outside Octomate is still visible and reusable inside it.

Two tiers do it, because no single source carries everything:

| | source | carries | misses |
|---|---|---|---|
| **hooks** | the client POSTs events to us | the prompt, the final answer, session bounds | thinking, tool calls, usage |
| **tailer** | we read the session's transcript file | the whole model timeline | nothing — but only once written |

The hooks are **live and lossless for the chat log**. The tailer is **complete but
trails**, because a turn is only provably finished when the file says so. Everything below
exists to make those two tiers agree.

This document is the Claude design plus the reasoning behind each rule, written so another
runtime (Codex) can build its own without re-paying for the mistakes. §8 is the part that
does not transfer.

---

## 1. What it produces

```
thread  (channel_tentacle_id="claude-native", chat_type="private", chat_id=<session id>)
  └─ conversation  (agent_tentacle_id="claude-native", external_id=<session id>)
       ├─ run <prompt_id>   ExternalAgentRun  msgs=221  bytes [278 … 725724]
       ├─ run <prompt_id>   ExternalAgentRun  msgs=220  bytes [727461 … 1472403]
       └─ run <prompt_id>   ExternalAgentRun  msgs=1    (no byte range — in flight)
  └─ thread_messages: inbound prompt / outbound answer, per turn
```

`claude-native` is a **synthetic id**, not a registered tentacle: an ingested thread is
recorded, never dispatched to. Nothing sends into it.

Two parallel records, deliberately:

- **the human ledger** (`thread_messages`) — the chat log a person would read.
- **the model timeline** (`agent_runs` → `model_messages`) — the context an agent would
  resume from.

`bind_ledger` cross-references them (`request_source` / `assistant_reply` bindings) so
each ledger row points at the model message it came from.

## 2. The one key that holds it together

**`prompt_id` is the per-turn key, and it is also the run id.**

Both tiers write under it, so they cannot disagree about what a turn is:

- ledger rows carry `platform_message_id = prompt_id`
- the run carries `id = prompt_id`

This is what lets the hooks sketch a turn and the tailer supersede it *as the same run*,
with no reconciliation step and no join table. **If your runtime has no stable per-turn id
spanning prompt→answer, stop and find one first** — everything downstream assumes it.

## 3. The tiers

### Hooks (live)

Only four events are handled, and only two carry content:

| event | why |
|---|---|
| `UserPromptSubmit` | the prompt; also starts the tailer (see below) |
| `Stop` | the turn's final answer |
| `SessionStart` | bounds the session — **but is not delivered to HTTP hooks**, so the first prompt does the starting instead |
| `SessionEnd` | finalizes the tailer |

Handlers are **synchronous** (Claude waits for our POST) — that is what guarantees
delivery before a short-lived `claude -p` exits — and return `{}`, deciding nothing. This
pipe only observes. `HOOK_TIMEOUT = 10` bounds it so a wedged Octomate can never freeze
someone's session.

### Tailer (live, byte-cursored)

One follow loop per session watches the transcript's **directory**, and on each change
reads forward from a byte cursor, framing on `\n`. Only complete lines are consumed; a
trailing fragment stays unread so a line split across two reads is never half-parsed. An
unmodeled or malformed line is skipped **but still advances the cursor**, so one bad line
can never wedge ingest.

Lines feed a `ClaudeRunAccumulator` — *the same translation the live tentacle uses*, so a
replayed run and a driven run produce identical message shapes. Reuse this; do not write a
second translator.

## 4. The invariants (the actual design)

**1. A turn commits on a *file* boundary — never on a hook.**
The next prompt line, or EOF at `finalize`. A hook says "the model stopped talking"; only
the file says "those bytes are flushed". Committing on `Stop` would race the writer.

**2. The byte range marks a turn finished.**
`end_offset` present → assembled from the transcript, final. Absent → provisional. This
single rule drives three things at once:
  - **idempotency** — re-recording an assembled run is a no-op, not a PK collision
  - **the sketch** (§5) — a run with no range may be replaced wholesale
  - **the guards** — `assembled(conversation)` seeds what a tail may skip

**3. Idempotency lives at the durable sink, not in the caller.**
An in-memory guard only knows the runs *it* wrote; it cannot see one another writer
committed. `record_external_run` checks the DB. We learned this the hard way: the
collision escaped into the follow loop's blanket `except`, which ended the loop and
silently stranded **every later turn of the session**.

**4. A commit that cannot be made ends the tail.**
Recovery resumes from the last committed turn's `end_offset`, which only points at the
right bytes while the committed turns are the *earliest* ones. Skipping a turn and tailing
on lets a later turn push that mark past the gap, stranding the skipped turn where no
recovery can reach it. Stopping keeps the mark honest.

**5. Only recovery resumes from the checkpoint; a live loop re-reads whole.**
Re-reading is what *heals* a session the tail was absent for, and it costs ~15 ms/MB
(measured: 161 ms for an 11 MB, 3987-line, 56-turn session — once per start, not per
event). Cheaper than the checkpoint's one real risk: resuming past bytes no run covered.

**6. One lock per session, shared by both tiers.**
The hooks' ledger writes and the tailer's run commits must not interleave for the same
session. `finalize` is called *outside* the lock — it awaits the loop's own last commit,
which takes the lock, so holding it would deadlock.

## 5. The provisional run ("sketch")

A turn only closes at the *next* prompt, which may be minutes away — so without this, a
turn in flight has a conversation and a chat log but **no run to hang a model history
from**, and nothing to reuse until it ends.

So the hooks write the run immediately, from what they alone can see:

```
UserPromptSubmit → run(id=prompt_id, messages=[ModelRequest(prompt)])    no byte range
Stop             → messages=[ModelRequest(prompt), ModelResponse(answer)] no byte range
turn closes      → messages=[…full timeline…]                            byte range set
```

Notes that cost us time:

- **`Stop` carries no prompt** — only `last_assistant_message`. Both hooks read the prompt
  back off the inbound ledger row.
- **Date the sketch** from the prompt's ledger row. `Conversation.runs` and `.messages`
  order on `started_at`, which is read off the first message — and pydantic-ai's
  `ModelRequest.timestamp` defaults to `None` (unlike `ModelResponse`). An undated run
  sorts *ahead of the whole history it belongs at the end of*.
- **Replace, don't merge.** The sketch is dropped whole (its messages cascade) and
  reinserted. `model_messages.run_id` is NOT NULL, so reassigning the collection would try
  to orphan the old rows and fail.

## 6. Failure handling

| situation | behaviour |
|---|---|
| Octomate restarts mid-session | next prompt restarts the tail from byte 0; the guard drops what's already committed |
| `SessionEnd` with no loop following | `finalize` falls back to `recover` — otherwise every turn since the loop died is silently dropped |
| session goes silent (crash, hooks removed) | idle reclaim after 30 min: drain, commit the trailing turn, drop the loop |
| commit can't take the lock | end the tail (invariant 4) |
| unmodeled transcript line | skip, advance the cursor |
| no consumer on the live stream | bounded, drop-on-full; durability never depends on a consumer existing |

`recover()` is the same assembly as the live loop, idempotent by `prompt_id`, and is the
manual path for a session Octomate watched only partially or never.

## 7. Do not ingest your own sessions

**The tentacle's own SDK sessions fire the operator's hooks.** The SDK loads
`~/.claude/settings.json` unless told otherwise (`setting_sources=None` means *load all*),
so Octomate's own runs arrive at its own hook pipe and get recorded a second time.

**Do not fix this by suppressing the operator's settings.** Their hooks — global or
project — are theirs to configure, and no flag skips one hook while keeping the rest
(only `disableAllHooks`, which would silence theirs along with ours). Nothing in a
tentacle should quietly override that.

Fix it by claiming the session instead:

```python
session_id = conversation.external_id or str(uuid7())   # settled BEFORE the CLI exists
with self.session_ingest.driving(session_id):
    async with ClaudeSDKClient(options=options) as client:   # resume=…, or session_id=…
        ...
```

- **Settle the id before launch** — resuming already names the session; a new one is
  pinned via `session_id` (the SDK takes one or the other, never both). No hook can then
  arrive unclaimed.
- **Bracket the run; don't follow its events.** The SDK's transport awaits the process on
  close, so when the claim drops, every hook the session can fire — `SessionEnd` last, on
  its way out — already has.
- **Count the claim.** A follow-up run supersedes a live one and the two overlap while the
  first unwinds; a plain set lets whichever ends first strip the claim from the one still
  driving.
- **No durability needed.** The claim is a cache of `conversation.external_id`, re-derived
  before every run; and a driven session cannot outlive Octomate (`__aexit__` interrupts
  live clients).

## 8. What does not transfer — Codex must answer these first

Everything above assumes four properties of the runtime. **Verify each against Codex
empirically — do not assume the Claude answer.**

1. **Is there a hook mechanism at all?** Claude Code POSTs HTTP hooks from settings. If
   Codex has no equivalent, the live tier disappears and only the transcript tier remains
   — which means no live ledger, and turns land only when the file says so.
2. **Is there a transcript on disk, append-only, one file per session?** The tailer's
   whole design rests on that (byte cursor, offsets, framing). Codex today uses
   `thread_start`/`thread_resume` with a thread id as `external_id`
   ([codex/base.py](../../octomate/tentacles/agent/codex/base.py)) — find out what, if
   anything, it writes and whether it is append-only.
3. **Is there a stable per-turn id spanning prompt→answer?** (§2.) Without it, re-key the
   design or invent one — but know that you have.
4. **Can a turn boundary be recognised in the file?** Claude's transcript marks a real
   human prompt with `promptSource != null`; tool-result user lines have it null. Some
   equivalent is needed, or turns cannot be framed.

Two traps worth naming, because they were invisible until measured:

- **Claude's own SDK sessions have `promptSource: null` and hook events with no
  `prompt_id`** — so an SDK session ingests as *junk* (unkeyed ledger rows, no runs), not
  as a duplicate. Whatever Codex's markers are, check them on a **driven** session and a
  **native** one, and confirm they differ the way you think.
- **A shipped CLI flag decides where hooks live.** `octomate claude hook install` defaults
  to `--scope user` but supports `--scope project`. Any design keyed on *where config
  lives* breaks for the other scope; keying on the session id does not.

## 9. Where to read

| concern | file |
|---|---|
| hook payload model, settings fragment, event list | [claude/hooks.py](../../octomate/tentacles/agent/claude/hooks.py) |
| live tier: ledger, sketch, tailer lifecycle, `driving` | [claude/ingest.py](../../octomate/tentacles/agent/claude/ingest.py) |
| transcript tier: follow loop, framing, turns, commit, recover | [claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py) |
| transcript line types, `prompt_text`, locating a session | [claude/transcript.py](../../octomate/tentacles/agent/claude/transcript.py) |
| the durable sink and its idempotency rule | `record_external_run` in [managers/conversation.py](../../octomate/managers/conversation.py) |
| the polymorphic run variant and its byte range | [schemas/runs.py](../../octomate/schemas/runs.py) |
| per-session locks | [claude/locks.py](../../octomate/tentacles/agent/claude/locks.py) |
| behaviour, pinned | [tests/agent/test_claude_tailer.py](../../tests/agent/test_claude_tailer.py), [tests/agent/test_claude_hook_ingest.py](../../tests/agent/test_claude_hook_ingest.py) |

The tests are the specification: each one names the failure it prevents.
