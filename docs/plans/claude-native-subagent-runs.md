# Plan: Claude native subagents as child runs

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-07-16
> **Builds on:** the shipped native-ingest stack — the transcript tailer
> ([claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py)), lazy restore
> ([claude/restore.py](../../octomate/tentacles/agent/claude/restore.py)), and the
> polymorphic `ExternalAgentRun` ([schemas/runs.py](../../octomate/schemas/runs.py)).

## TL;DR

Today both the tailer and restore **skip** every transcript line with `isSidechain =
true` — a subagent's (Task tool's) internal turn. The skip is deliberate and safe (a
subagent line can never open or fold into a top-level turn; the byte cursor still advances
past it), so the parent run's timeline is clean. But the subagent's own work — its prompt,
thinking, tool calls, and result — is **dropped entirely**. This plan captures each
subagent invocation as a **child run** hanging off the parent turn's run, so the full tree
of a native session is reconstructable, live and on restore.

## Why now — the gap

| | Parent turn | Subagent turn (`isSidechain`) |
|---|---|---|
| Recorded as a run? | ✓ `ExternalAgentRun` | ✗ skipped |
| Model timeline (thinking/tools/result) | ✓ | ✗ dropped |
| Byte range checkpointed | ✓ offsets | ✗ (cursor advances, not recorded) |
| Reconstructable later | ✓ | ✗ |

A subagent can do the bulk of a turn's real work (a research fan-out, a parallel edit
sweep). Dropping it leaves the persisted timeline materially incomplete, and the human
ledger's answer often summarizes work that has no recorded provenance.

## What already exists (grounding)

- **The skip** — both `split_turns` ([restore.py](../../octomate/tentacles/agent/claude/restore.py))
  and the tailer's `process_line`
  ([tailer.py](../../octomate/tentacles/agent/claude/tailer.py)) short-circuit on
  `line.is_sidechain` before any turn logic, for both user and assistant lines.
- **Transcript shape** — every `TranscriptSessionLine` carries `is_sidechain`,
  `parent_uuid`, and `uuid` ([claude/transcript.py](../../octomate/tentacles/agent/claude/transcript.py)).
  A subagent line chains to its siblings by `parent_uuid`; the invoking `Task` tool call
  lives on a **non-sidechain** assistant line in the parent turn, and its `tool_use_id`
  correlates to the sidechain lineage (exact linkage to confirm empirically — see Risks).
- **The run model** is polymorphic and single-table
  ([models/runs.py](../../octomate/models/runs.py)); a child variant or a self-referential
  parent link fits without a second table.
- **The accumulator** (`ClaudeRunAccumulator`) already rebuilds a full model timeline from
  a slice of transcript lines — a subagent turn is just another slice fed to a fresh
  accumulator.

## Design sketch (to be firmed up)

1. **Group sidechain lines into subagent turns.** Instead of dropping `is_sidechain`
   lines, route them into a per-lineage buffer keyed by their root `uuid`/`parent_uuid`
   chain. A subagent turn opens on its first line and closes when the lineage goes quiet
   or the parent turn commits.
2. **Model the parent link.** Add a nullable `parent_run_id` (self-FK) to `AgentRun`, or a
   dedicated `SubagentRun` polymorphic identity, so a child run points at the parent
   turn's run. Reuse the offset columns for the child's own byte range.
3. **Reuse one assembler.** After UoW-D extracts the shared turn assembler, both the parent
   and child turns run through it; the only difference is the parent link and that a child
   never binds the human ledger (there is no human prompt/answer for a subagent).
4. **Bind the invocation.** Correlate the child run to the parent's `Task` `ToolCallPart`
   (by `tool_use_id`), so a UI can expand a tool call into its subagent's full timeline.
5. **Live + restore parity.** The tailer emits child-run commits the same way it emits
   parent turns; restore rebuilds them from the same lines. Idempotent by the child's own
   `prompt_id`/`uuid`.

## Non-goals

- Nested subagents beyond one level, until the transcript is confirmed to represent them.
- Streaming subagent events to the live UI as a separate lane — the durable child run is
  enough for v1; live fan-out rendering is later.

## Risks / unknowns

| Risk | Mitigation |
|---|---|
| **Exact parent↔child linkage in the transcript is unverified** — how a sidechain lineage ties back to its invoking `Task` tool call (via `tool_use_id`, `parent_uuid`, or a root marker) needs empirical confirmation on a real multi-subagent session. | Start by dumping a real transcript with subagents and mapping the `uuid`/`parent_uuid`/`tool_use_id` graph before writing any grouping logic. |
| **Turn-boundary interplay** — a subagent runs *inside* a parent turn, so its lines interleave between the parent's assistant/tool-result lines. The parent turn's offsets must stay contiguous while the child's carve out their own range. | Keep the parent assembler ignoring sidechain lines (as today) for its own byte range; the child assembler owns the sidechain ranges independently. Ranges may be non-contiguous — never assume otherwise. |
| **Unpaired tool calls in a child** — same history-reuse hazard as parent turns: a subagent interrupted mid-tool-call yields an unpaired `ToolCallPart`. | Trim a trailing unpaired tool call before committing the child run, mirroring the parent path. |
| **Ordering vs. the parent commit** — a child run should exist before (or with) the parent turn it belongs to, so the parent's `Task` call can bind to it. | Commit child runs as their lineage closes, before the parent turn's boundary commit; bind on the parent commit. |

## Acceptance

A native session that spawns a subagent records a parent `ExternalAgentRun` **and** a
child run linked to it, the child carrying the subagent's thinking/tool/result timeline
with its own byte range; restoring the same transcript reproduces the identical parent +
child tree; a session with no subagents is unchanged.
