# Plan: Subagent runs — commissioned agents, claimed routes, and native subagent ingest

> **Status:** in progress · **Owner:** @luhui · **Created:** 2026-07-17
> **Shipped:** §1b abort fix (`2c9c3cc`) · UoW-1 run tree (`f90394f`) · UoW-4 subagent
> transcripts (`7241206`) · UoW-5 subagent hooks (`56c1c5b`) + live settle fix
> (`5ac54e0`). **Live-verified** end to end against a running server on 2026-07-18 —
> findings folded into §6. UoW-2/3 deferred by owner; **UoW-6 is handed to Codex** —
> its brief is §7 + the UoW-6 section, written to need no other context.
> **Supersedes:** `claude-native-subagent-runs.md` (2026-07-16) — renamed and rewritten; its
> central premise was measured false (§1) and its scope was one quarter of this.
> **Read first:** **§1b — a live 24 % data-loss bug in Codex ingest, found while writing
> this. It is not a subagent bug. Ship its fix before this plan.**
> **Builds on:** the shipped native-ingest stack
> ([native-session-ingest.md](done/native-session-ingest.md)) and the self-routing gate
> ([self-routing-dispatch.md](done/self-routing-dispatch.md)).
>
> **Every claim below about what a runtime writes was measured** — 80 Claude transcripts and
> 229 Codex rollouts on this machine — not reasoned from docs. Two designs and one shipped
> branch were already built on guesses that the corpus falsified (§1, §5). Where something is
> *not* verified, it says so and says how to check.

## TL;DR

An agent can hand a conversation **away** (`summon` — sticky, the caller's turn is over) or
take it **elsewhere** (`teleport` — same agent, new thread). It cannot ask another agent to
do a piece of work and report back. That is a **subagent**, and nothing in Octomate has one:
not the gate, not the run model, not the ingest — even though Claude and Codex spawn them
natively all day and we throw every byte away.

This plan makes a subagent a first-class thing in one shape, fed from two sources:

| | who spawns it | what we do today | what this plan does |
|---|---|---|---|
| **Octomate** | inkling calls `commission` | ✗ no such tool | run it, record it as a child run |
| **native** | Claude's `Agent` tool / Codex's `spawn_agent` | ✗ dropped — and Codex's is dropped for a reason that was **made up** (§5) | ingest it as a child run |

Both land as the same **child run** under the same parent link, so a subagent tree reads the
same whether Octomate drove it or merely watched it. That shared spine is **UoW-1**, and
everything else hangs off it.

---

## 1. The correction — measured, not reasoned

The superseded plan's design rested on this claim:

> *"Today both the tailer and restore **skip** every transcript line with `isSidechain =
> true` — a subagent's (Task tool's) internal turn … the subagent's own work is dropped
> entirely."*

The skip is real ([tailer.py:409](../../octomate/tentacles/agent/claude/tailer.py#L409),
[:417](../../octomate/tentacles/agent/claude/tailer.py#L417)). **The lines it skips do not
exist.** Measured across the whole local corpus — 80 main transcripts, Claude Code 2.1.177
through 2.1.211, 27 sessions with subagents:

```
main transcripts carrying inline `isSidechain` lines ...... 0
sessions with a subagents/ directory ..................... 27
```

Since at least 2.1.177, a subagent writes **its own file**:

```
<project>/<session-id>.jsonl                                ← parent. no sidechain lines. ever.
<project>/<session-id>/subagents/agent-<agentId>.jsonl      ← one file per subagent. documented.
<project>/<session-id>/subagents/agent-<agentId>.meta.json  ← {agentType, description, toolUseId, spawnDepth}
                                                               undocumented. we do not read it — see below.
```

So the superseded design — *"route sidechain lines into a per-lineage buffer keyed by their
root `uuid`/`parent_uuid` chain"* — reconstructs a lineage that the runtime already
reconstructed and wrote to a named file. **Do not build it.** The `is_sidechain` guard stays
as a cheap defence against an older transcript, and stops being load-bearing.

The plan also flagged its own biggest risk as *"exact parent↔child linkage in the transcript
is unverified."* It is now verified, and over-determined — which lets us **pick the
documented one and throw the rest away**:

| linkage | where | documented? | verdict |
|---|---|---|---|
| **`promptId` on every subagent line = the parent turn's `prompt_id`, which *is* the parent run's PK** | the subagent transcript | **yes** — a common field, and the same per-turn key Codex spells `turn_id` | **the linkage of record** |
| `toolUseResult.agentId` on the parent's tool-result line | the parent transcript | yes | kept for one field only — see below |
| `toolUseId` → the parent's `Agent` tool call | the `.meta.json` sidecar | **no** — undocumented internal | **dropped** |

**`promptId` is the linkage.** It is the key the whole ingest design already turns on
([native-session-ingest.md §2](done/native-session-ingest.md)) — a subagent line simply
carries its *parent's*. Nothing else is needed to hang a child run under a parent run.

**Dropping the sidecar costs nothing**, which is the point: everything it carries is
available from a documented source.

| sidecar field | documented replacement |
|---|---|
| `toolUseId` | `toolUseResult.agentId` on the parent's tool-result line — a line the accumulator **already parses** into a `ToolReturnPart`. Free. |
| `agentType` | `SubagentStart.agent_type` (a documented hook field) |
| `spawnDepth` | walk our own `parent_run_id` chain — and it is already absent on 59 of 93 local files, so it was never dependable |

> **One consequence to confirm.** `promptId` alone gives the parent **run**, not which of a
> turn's N `Agent` calls a child answers — a real case: this plan's own research turn
> spawned three subagents under one `promptId`. The only documented source of that binding
> is `toolUseResult.agentId`, which is why the table keeps it for `parent_tool_call_id` and
> nothing else. **If you want that row gone too, `parent_tool_call_id` goes with it** and a
> UI can only expand a *turn* into its subagents, never a tool call into its subagent.

Three more corrections, cheaply won:

- **The tool is `Agent`, not `Task`** — renamed in v2.1.63. The old plan says `Task` throughout.
- **`restore.py` / `split_turns` do not exist** — merged into `ClaudeTranscriptTailer.recover`
  (commit `3d20a19`). The old plan's grounding cites both. There is **one** skip site, not two.
- **`SubagentStart` and `SubagentStop` exist**, both HTTP-deliverable, and mirror the
  `UserPromptSubmit`/`Stop` pair the live tier already runs on (§UoW-5).

## 1b. A live bug this plan uncovered ✅ shipped first, on its own (`2c9c3cc`)

Chasing Codex's subagent story found a **shipped, silent, 24 % data-loss bug in Codex
ingest**. It is not a subagent bug and must not wait for a subagent feature.

**A Codex turn closes on `task_complete` *or* `turn_aborted`. We only handle the former**
([codex/tailer.py:260-266](../../octomate/tentacles/agent/codex/tailer.py#L260-L266)). An
aborted turn therefore never closes, `state.open_turn` never clears — and then the nesting
branch turns a stuck turn into a **cascading** one: every subsequent `task_started` is
misread as a nested subagent, pushed onto `nested_turn_ids`, and swallowed **for the rest of
the session**.

Measured by replaying our own state machine over all 229 local rollouts:

```
real task_complete .......... 10138
turns our tailer emits ....... 7672
turns LOST ................... 2466   (24.3%)
files affected ................. 64 / 229      worst single file: 737 of 830 turns lost
```

Independently confirmed: `turn_aborted` occurs **131 times across 63 files**, and every
opened turn's fate corpus-wide is `10138 task_complete`, `131 turn_aborted`, `20 dangling`.
Of the 64 broken files, **56 jam on `turn_aborted`**, 7 on a dangling turn, 1 on a real overlap.

```json
{"type":"turn_aborted","turn_id":"019f5939-0a5b-7520-8e12-168fcdace93a",
 "reason":"interrupted","completed_at":1783908340,"duration_ms":2358}
```

**The fix is two lines and one guard, and it recovers ~24 % of Codex turns on its own:** close
the open turn on `turn_aborted` as well as `task_complete`, and never let a stuck `open_turn`
silently absorb later turns. Do it now, separately. UoW-6 then deletes the nesting branch that
made a stuck turn cascade, which is why the bug is *findable* here but not *owned* here.

## 2. What the four asks actually share

| # | ask | needs |
|---|---|---|
| 1 | inkling can call any agent tentacle as a subagent | a tool, a runner, **the run tree** |
| 2 | every agent tentacle claims its ability, effort, cost | a claim model on the route |
| 3 | thread→conversation→run→message accepts subagent structure | **the run tree** |
| 4 | native session sync ships subagent hooks + transcripts | ingest, **the run tree** |

Three of four converge on **the run tree**. It ships first, alone, and the other tracks are
independent of each other after it.

## 3. What already exists (grounding)

- **The spellbook.** `GateCapability`
  ([gate.py:73](../../octomate/capabilities/gate.py#L73)) contributes an instruction plus
  three tools: `scry` (list routes), `summon` (sticky handoff — records a decision the
  reflex graph reads *after* the run ends), `teleport` (deferred; the graph forks history
  and resumes the same agent elsewhere). **`commission` is the missing fourth.**
- **Tool calls already run concurrently.** pydantic-ai executes a response's tool calls in
  parallel by default (`_parallel_execution_mode_ctx_var` defaults to `'parallel'`,
  `pydantic_ai/tool_manager.py:40-41`; each call becomes an `asyncio.create_task`,
  `_agent_graph.py:1915-1925`). **Fan-out needs no machinery of ours** — this is the fact
  that collapses UoW-3 to a single tool function.
- **A tool already knows its own coordinates.** `RunContext.run_id`
  (`pydantic_ai/_run_context.py:86`) and `RunContext.tool_call_id` (`:62`) are exactly the
  `parent_run_id` and `parent_tool_call_id` the run tree wants, available in the tool body
  with nothing threaded through.
- **The one wait that must never be unbounded** is already named: `approval_timeout`
  ([config/agents.py:113](../../octomate/config/agents.py#L113)) exists because a human may
  never answer. A commissioned agent may never finish, for the same reason and with the same
  fix.
- **The route.** `SummonRoute(agent_id, model, description)`
  ([triage.py:43](../../octomate/schemas/triage.py#L43)), built from
  `ReflexDeps.available_routes` ([graph.py:145](../../octomate/reflex/graph.py#L145)). The
  `description` is one free-text string **per tentacle class**
  ([agent/base.py:52](../../octomate/tentacles/agent/base.py#L52)), shared across every
  model row of that agent. That is the entire existing notion of "what can this agent do".
- **An in-process agent's approval resolves without the graph.** `Octomate.kick` hands a
  `DeferredActionBatchResponse` straight to `agent.pending[batch_id]`
  ([base.py:142-155](../../octomate/base.py#L142-L155)) for any agent with
  `in_process = True` (claude, codex). This is why a commissioned claude can still ask a
  human for permission while its parent's tool call waits — and why a commissioned
  **inkling** asking a *question* cannot (UoW-3's deadlock).
- **~~Nesting prior art, in Codex.~~** An earlier draft said `codex/tailer.py:216-245`'s
  `nested_turn_ids` stack meant "the boundary detection is done." **It detects a thing that
  does not happen** — see §5. It is not prior art; it is a bug.
- **A render slot nothing fills.** `StreamBlockType`
  ([feelers/output.py:152](../../octomate/tentacles/channel/feelers/output.py#L152)) already
  reads `Literal["answer", "thinking", "tool_call", "tool_result", "subagent"]`. Nothing in
  the ingest or react path has ever produced a `subagent` block.

## 4. The shape it produces

```
thread
 ├─ conversation  (agent=claude-native, subagent_id="", external_id=<session>)
 │    ├─ run <promptId-1>   ExternalAgentRun   bytes[278 … 725724]  of <session>.jsonl
 │    └─ run <promptId-2>   ExternalAgentRun   bytes[727461 … …]    of <session>.jsonl
 │         └── ToolCallPart(tool_name="Agent", tool_call_id=toolu_01J1…)
 │                                                    ▲
 └─ conversation  (agent=claude-native, subagent_id=<agentId>, external_id=<agentId>)
      │                                  ▲ the SUBAGENT — stable across its turns
      ├─ run <agentId>:<promptId-2>  ExternalAgentRun  bytes[0 … 301991]
      │        of subagents/agent-<agentId>.jsonl
      │        parent_run_id       = <promptId-2>
      │        parent_tool_call_id = toolu_01J1…  ───────────┘
      └─ run <agentId>:<promptId-5>  ExternalAgentRun  bytes[301991 … …]
               same file — the subagent was resumed by a LATER parent turn (§4a),
               so parent_run_id = <promptId-5>. Same conversation: one context.
```

The same shape, Octomate-driven:

```
thread
 ├─ conversation  (agent=inkling, subagent_id="")
 │    └─ run <uuid7>   AgentRun
 │         └── ToolCallPart(tool_name="commission", tool_call_id=pyd_ai_01…)
 └─ conversation  (agent=codex, subagent_id="repo-audit")
      │                             ▲ the name the model chose, its handle for `commune`
      ├─ run <uuid7'>   AgentRun   parent_run_id=<uuid7>,  parent_tool_call_id=pyd_ai_01…
      └─ run <uuid7''>  AgentRun   parent_run_id=<uuid7b>, parent_tool_call_id=pyd_ai_02…
               `commune("repo-audit", …)` in a later parent turn — same conversation,
               so it answers from everything it already did (§4a).
```

The same shape as the two native trees, reached by a third road. What differs is only what
plays the part of `subagent_id`: Claude's `agentId`, Codex's child `thread_id`, and here a
name a model chose and can say out loud.

And native Codex — the same shape, reached by a different road (§5):

```
thread
 ├─ conversation  (agent=codex-native, subagent_id="", external_id=<thread-id>)
 │    └─ run <turn_id>   ExternalAgentRun   bytes of rollout-…-<thread-id>.jsonl
 │         └── event_msg sub_agent_activity  event_id=call_47up…  agent_thread_id=019f6b28…
 │                                                    ▲
 └─ conversation  (agent=codex-native, subagent_id=<child thread-id>, external_id=<child thread-id>)
      └─ run <child turn_id>   ExternalAgentRun   bytes of rollout-…-<child thread-id>.jsonl
           parent_run_id       = <turn_id>
           parent_tool_call_id = call_47up…  ───────────┘
```

Note what is *not* here: no `session_id` anywhere. Codex shares it across the whole session
tree, so it names the tree and can never key a conversation.

**A child gets its own conversation.** Not an aesthetic choice — three forcing reasons:

1. **`Conversation.messages` is an unfiltered viewonly join through `agent_runs`**
   ([conversation.py:70-78](../../octomate/models/conversation.py#L70-L78)). A child run in
   the parent's conversation flattens its whole timeline into the parent agent's model
   history, on the next turn, silently. That is context poisoning, not a display bug.
2. **`external_id` is one handle per conversation.** The child has its own transcript file
   and needs its own handle; there is no second slot.
3. **`recover()` resumes from `max(end_offset)`**
   ([tailer.py:273](../../octomate/tentacles/agent/claude/tailer.py#L273)). Parent and child
   offsets index **different files**. Mixed in one conversation, one `max()` spans two
   coordinate systems and strands turns — the precise failure invariant 4 of the ingest
   design exists to prevent. Separate conversations keep each `max()` inside one file.

## 4a. How many runs is a subagent? And can it be deferred?

Two questions that look like detail and are actually the shape of the model. Both were
answered by measuring, and the first answer is **not** what an earlier draft assumed.

### A subagent is *one conversation*, and **one-to-many** runs

| | runs per subagent | resumable? | evidence |
|---|---|---|---|
| **commissioned** (Octomate) | **1 … N** | **yes** — `commune` (UoW-3) | by design, following the two below |
| **Claude native** | **1 … N** | **yes** | measured: 4 of 93 local subagent transcripts carry 2–3 `promptId`s |
| **Codex native** | **1 … N** | **yes** | a subagent *is* a thread; `send_message`, `followup_task`, `wait_agent` exist to continue it |

**One row, three times** — which is the point. Both runtimes converged on *spawn returns a
handle; a second tool continues it with history intact*, and a commission that could not do
that would be the odd one out for no reason. The model below is shaped to fit all three, so
"commissioned" is no longer the degenerate case.

And a resumed subagent **keeps its history** — it is not a fresh context reusing a filename.
Measured on `agent-aefc87000c9224858.jsonl`, at the `promptId` boundary the new prompt's
`parentUuid` points at **a line earlier in the same file** — the subagent's own prior
assistant response. The chain is continuous across the turn boundary.

```
agent-<agentId>.jsonl          one file, one subagent, one continuous chain
  ├── promptId=41e17c0b …      ← spawned by parent turn A   → run <agentId>:41e17c0b
  │     …assistant, tools…                                     parent_run_id = A
  └── promptId=dfbb3aa9 …      ← resumed by parent turn B   → run <agentId>:dfbb3aa9
        parentUuid ─┘ points back into turn A's lines          parent_run_id = B
        (this is what proves the history carried over)
```

So the two ids do different jobs, and collapsing them is the bug:

- **`agentId` names the subagent** — stable across its turns → **the conversation**.
- **`promptId` names the parent turn that drove this one** — varies → **the run's `parent_run_id`**.

The child run key `<agentId>:<promptId>` is exactly *(which subagent, which parent turn)*.
This is why the conversation cannot be keyed on the parent run, and why `commune` (UoW-3)
costs almost nothing to add: **the model already had room for it.** A resumed subagent is a
second run in the same conversation on every one of the three roads.

### No subagent run is ever deferred — on any runtime

A stronger claim than "we avoid it", and it holds for a different reason per agent:

| agent | can its run return `DeferredToolRequests`? | why |
|---|---|---|
| **claude / codex** (commissioned or native) | **architecturally no** | they present approvals via `channel.feelers.present_actions` directly and park a future in `self.pending` ([claude/base.py:222](../../octomate/tentacles/agent/claude/base.py#L222), [codex/base.py:376](../../octomate/tentacles/agent/codex/base.py#L376)), resolved by `kick`. They **accept `deferred_suspender` and ignore it**, like every other pydantic-ai knob. A blocked in-process run is not a deferred run. |
| **inkling** (commissioned) | yes — **so we take the tools away** | it is the only agent that defers at all. UoW-3 gives a commissioned run no suspender and no `ask_questions`; without them nothing can raise `CallDeferred`. |
| native subagents (either runtime) | not our problem | the client asks its own human in its own terminal. We are recording, not driving. |

Two consequences worth stating because they look like contradictions and are not:
**removing the suspender does not break claude's approval cards** (it never used one), and a
commissioned claude *can* still stop and ask a human for permission — that is the one thing a
commission legitimately borrows the parent's surface for (UoW-3).

## 5. Codex — the same questions, different answers

[native-session-ingest.md §8](done/native-session-ingest.md) told Codex to answer four
questions empirically rather than assume Claude's answers. Nobody did, and a **guess got
written into the code as a comment** — which this plan then inherited as fact. Here are the
answers, measured against 229 local rollouts and the docs.

> The comment that misled two plans, [codex/tailer.py:221-224](../../octomate/tentacles/agent/codex/tailer.py#L221-L224):
> *"Subagents run inside their parent's turn and emit their own task boundaries into the same
> rollout."* **Both clauses are false.** They do not run inside the parent's turn, and they
> emit nothing into the parent's rollout. Delete it with the branch it justifies.

| | Claude | Codex |
|---|---|---|
| subagents exist? | yes, the `Agent` tool | **yes** — `spawn_agent` in the `collaboration` namespace, **on by default** |
| the model calls | `Agent` | `spawn_agent`, `wait_agent`, `send_message`, `interrupt_agent`, `list_agents`, `followup_task` |
| child transcript | `<session>/subagents/agent-<agentId>.jsonl` — **a named, obvious location** | `sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl` — **indistinguishable from a normal session by path** |
| how to recognise a child | the directory it is in | **only** by parsing `session_meta` |
| child → parent link | `promptId` (= the parent run's PK) | `session_meta.source.subagent.thread_spawn.parent_thread_id` |
| parent → child link | `toolUseResult.agentId` | `event_msg` / `sub_agent_activity` → `event_id` **is** the spawning tool call id |
| the shared id | `sessionId` is the parent's session | `session_id` is shared by the **whole session tree** — it names the tree, not a thread |
| per-turn key | `promptId` | `turn_id` — but **not on `user_message` / `agent_message`**; ~85 % coverage, so the `task_started`/`task_complete` bracket still does the associating |
| byte ranges | parent and child in **different files** | **also different files** — the earlier "one interleaved coordinate space" claim was pure fiction |
| depth | `spawnDepth`, absent on 59/93 | `depth` + `agent_path` (`/root/final_di_review`); `agents.max_depth` **defaults to 1**, `max_threads` to 6 |
| the child's prompt | its first transcript line **is** the prompt | **encrypted.** `spawn_agent`'s `message` is a `gAAAAAB…` blob; only `SubagentStart` carries the prompt |
| noise | none — a subagent is a subagent | **110 of 229 local rollouts are `subagent \| guardian`**, an internal reviewer. Only **15** are real delegations |
| hooks we handle | 3 | **3 of 10** |
| subagent hook path | `transcript_path` — **ambiguous**, parent's or child's (UoW-5) | **`agent_transcript_path` — explicit and unambiguous.** Better than Claude's. |

**So Codex converges with Claude where it counts and diverges where it hurts.** Both put a
child in its own file, so UoW-1's separate-conversation choice holds for both, and
`max(end_offset)` stays inside one file on both. The divergences that cost real work are all
Codex-only: **discovery is by content, not path**; **the guardian flood** must be filtered;
and **fork replay** duplicates parent turns into the child's file.

---

## 6. As built — the Claude reference implementation

Everything below is **shipped and live-verified** (4 real subagents against a running
server). This is the design UoW-6 mirrors; read the code it names before writing any.

### The map

| mechanism | where | the one sentence that matters |
|---|---|---|
| child cursor | `SubagentTail` in [claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py) | a child is **state on the session's tail, not a task** — own byte cursor, conversation, open turn; nothing to leak or orphan |
| discovery | `pump_subagents` | list `<session>/subagents/agent-*.jsonl` on every pump; the subagent pass runs **outside** the parent's no-new-bytes early return, because parent-quiet is when children are busiest |
| liveness | `pump` + `poke_subagent` | children ride the parent's wake (events + 60s poll tick); hooks add precise pokes; child bytes also reset the idle-reclaim clock |
| turn framing | `process_subagent_line` | close/open on **`promptId` change only** — child files have no `promptSource`, and a resumed child's turn 2 opens on a *tool-result* line (measured), so `begin_subagent_turn` seeds a prompt only when there is text and always consumes the opener |
| run key | `close_subagent_turn` | `<agentId>:<promptId>` = *(which subagent, which parent turn)*; a bare `promptId` would collide with the parent run it belongs to |
| parent links | same + `harvest_subagent_call` | `parent_run_id` = the child lines' own `promptId`; `parent_tool_call_id` harvested from the parent's `toolUseResult.agentId` tool-result line |
| conversations | `pump_subagent` → `ensure(subagent_id=…, parent_conversation_id=…)` | child context is its own conversation; the manager refuses half-set identity |
| ledger | — | children **never** touch it: no prompt, no answer, no rows |
| hook dispatch | `handle` in [claude/ingest.py](../../octomate/tentacles/agent/claude/ingest.py) | any event carrying `agent_id` that is not `SubagentStart/Stop` fired *inside* a subagent → dropped before parent handlers |
| start | `on_subagent_start` | self-heals the session tail (a subagent proves its session is live), sketches iff `prompt_id` present, pokes the child |
| stop | `on_subagent_stop` → `finish_subagent` | the child's finalize: drain, **settle**, commit; with nothing following → `recover` (which walks the subagents dir itself) |
| path normalization | `session_transcript_path` | a subagent event may name the child's own file; derive the session's from it — never start a session tail on a child path |
| the settle guard | `finish_subagent` + `subagent_settled` | see finding 2 below — this is the part a naive port will get wrong |
| first-sighting race | `ensure_lock` in [managers/conversation.py](../../octomate/managers/conversation.py) | a hook poke and the follow task's prepare race one INSERT; the loser becomes a cache hit under the lock, not an IntegrityError that kills the loop |

### Live findings (2026-07-18, measured — not theory)

1. **The hooks fire.** `SubagentStart`/`SubagentStop` reached the http handler from a
   session started *before* the hook reinstall; children committed while the parent turn
   was still a provisional sketch, with exact `parent_tool_call_id` linkage.
2. **The final answer line races `SubagentStop`, and usually loses.** 2 of the first 3
   children committed permanently missing their conclusion (~1–2KB) — a byte-ranged run
   is final, so the loss is unrecoverable. The fix (`5ac54e0`): drain until the file
   yields the event's own `last_assistant_message` (exact match, instant when it works —
   measured `lost=0` post-fix), or the writer goes quiet for two 200ms polls (the
   content-agnostic fallback — **no contract promises the hook payload equals the
   transcript byte-for-byte**), bounded at 2s; and never commit a turn the settle-window
   pump already replaced (a resume arriving mid-settle).
3. **`SubagentStart` carries no `prompt_id`.** Polled the DB through a child's whole
   lifetime: no sketch ever appeared; the run materializes complete at Stop. The sketch
   path is dormant-by-reality; degradation is the live path.
4. **Still unverified:** whether the events' `transcript_path` is the child's file or the
   session's (`session_transcript_path` accepts both, so it has not mattered), and
   whether `SubagentStart` re-fires for a resumed subagent.

## 7. UoW-6 working instructions — for Codex, from Claude

You are implementing UoW-6 (below) in this repo without the conversation that produced
this plan. What you need:

- **Read first:** §5 (your runtime's measured answers), §6 (the reference implementation
  you are mirroring), then the UoW-6 section (your work items). The claims about rollout
  shapes come from a 229-file local corpus; where the plan says *measured*, trust it over
  intuition, and where it says *unverified*, verify before building on it.
- **Mirror the shape, not the letter.** Your runtime differs where §5 says it differs
  (discovery by `session_meta`, the guardian filter, fork replay, encrypted prompts, an
  explicit `agent_transcript_path`). Everything else — child-as-cursor, own conversation
  per child, run key *(which subagent, which parent turn)*, no ledger rows, settle before
  commit — transfers as designed.
- **The settle race applies to you.** Codex's `SubagentStop` also announces
  `last_assistant_message`, and your rollout writer is also not synchronous with the
  hook. Reuse the Claude settle pattern (fast-path match → byte quiescence → bounded
  timeout) rather than trusting the file at Stop; Claude's version is
  `finish_subagent` in [claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py).
- **House rules:** AGENTS.md at the repo root governs style and process — surgical
  changes, imports at top, no `typing.Any`, fail-fast. Verify with
  `uv run pytest -q && uv run ruff check . && uv run pyright` (29 pyright errors are
  pre-existing; the bar is *no new ones*). Tests are the specification: name each for
  the failure it prevents, and delete `test_nested_task_does_not_close_or_pollute_the_parent`
  rather than adapting it — it pins the fiction step 0 removes.
- **Do not commit.** Leave the working tree for the owner's review; report what changed
  and how it was verified.

## UoW-1 — the run tree ✅ shipped (`f90394f`)

The spine. Ships alone; nothing below is possible without it.

- **ORM** ([models/runs.py](../../octomate/models/runs.py)) — on `AgentRun`, so **both**
  variants carry it (an octomate child and an external child are the same relationship):
  - `parent_run_id: Mapped[str | None]` — self-FK to `agent_runs.id`, indexed.
    **`String`, not `Uuid`** — the PK is a pydantic-ai run id, not a native uuid column.
  - `parent_tool_call_id: Mapped[str | None]` — the `ToolCallPart.tool_call_id` in the
    parent's timeline this child answers. Binds a tool call to its subagent's run.
- **ORM** ([models/conversation.py](../../octomate/models/conversation.py)):
  - `subagent_id: Mapped[str]` — indexed, **not** a FK, **empty (not NULL) for the
    agent's own conversation** — the `Thread.thread_id` sentinel convention, which is
    what lets the existing unique constraint simply widen to three columns. Set = one
    subagent's context; the value is whatever the runtime calls that subagent (§4a):
    Claude's `agentId`, Codex's child `thread_id`, a commission's name.
  - ⚠ **It must not be `parent_run_id`, and this is not obvious.** A subagent can be
    *resumed by a later parent turn* (§4a) — its runs then carry **different**
    `parent_run_id`s, so keying the conversation on the parent run would split one
    continuous context across two conversations, each claiming to be "one agent's model
    context". The subagent's own id is stable across its turns; the parent run is not. The
    parent link belongs on the **run**, per turn, where it already is.
  - `parent_conversation_id: Mapped[uuid.UUID | None]` — self-FK (SET NULL), set iff
    `subagent_id` is; the manager refuses half-set states, so there is no separate
    `is_subagent` flag to drift. **Why it is not derivable from the runs:** a
    commission's parent conversation belongs to a *different agent* (inkling spawns
    codex — nothing else on the child row names inkling), and the parent's run row
    does not exist until that run *finishes*, so run-hopping fails exactly while a
    commission is in flight. Conversation carries the stable whose-context half;
    the run keeps the per-turn which-call half (`parent_run_id` +
    `parent_tool_call_id`) — a resumed subagent has one parent conversation but a
    different parent run per turn, which is why neither level can absorb the other.
  - **The trap the sentinel dodges:** with a *nullable* `subagent_id`, widening the
    unique constraint to three columns **does not work** — SQL treats NULLs as distinct
    in a unique constraint, so `(T, claude, NULL)` twice would not collide, silently
    destroying one-conversation-per-agent. That road needs two indexes (a partial
    `UNIQUE … WHERE subagent_id IS NULL` plus the full one). The empty-string sentinel
    is a value, so the one three-column constraint enforces both halves — and it is the
    convention the schema already uses (`Thread.thread_id = ""` for a main surface).
  - **Not `external_id`, though they often hold the same string.** For a native child both
    are the `agentId`/`thread_id`. They are different concepts: `external_id` is a
    *resumable handle*, rewritten on every `persist_run`
    ([conversation.py:219-221](../../octomate/managers/conversation.py#L219-L221)); a
    mutable column has no business in a unique index. And a commissioned child has an
    identity but no external session at all.
- **Read paths that must be taught the child exists** — each is a live bug the moment
  UoW-1 lands, not a nicety:

  | site | today | after |
  |---|---|---|
  | `Conversation.messages` ([conversation.py:70](../../octomate/models/conversation.py#L70)) | joins every run | unchanged — children live elsewhere, so it is already correct |
  | `Conversation.runs` ([:57](../../octomate/models/conversation.py#L57)) | `order_by=started_at`, **nullable** | a child with `started_at=None` sorts ahead of the whole history. Date children the way the sketch is dated ([native-session-ingest.md §5](done/native-session-ingest.md)) |
  | `AgentRun.messages` ([runs.py:63](../../octomate/models/runs.py#L63)) | `cascade="all"`, **not** `delete-orphan` | deleting a parent run will not reach its children; `model_messages.run_id` is NOT NULL, so a half-cascade raises `IntegrityError`. `record_external_run`'s delete-whole path ([conversation.py:172](../../octomate/managers/conversation.py#L172)) must account for children before it drops a sketch |
  | `assembled()` ([tailer.py:60](../../octomate/tentacles/agent/claude/tailer.py#L60)) | per conversation | already correct once children are in their own conversation |

- ⚠ **The live-client maps must be re-keyed, and this is the sharpest edge in UoW-1.**
  Both in-process tentacles cache their live client **by `thread_id`**, on an assumption
  this UoW deletes. Claude states it outright
  ([claude/base.py:141-143](../../octomate/tentacles/agent/claude/base.py#L141-L143)):
  *"keyed by thread_id — this tentacle owns one agent id, **so a thread names its
  conversation**"*. After UoW-1 a thread names **many** of its conversations.

  | | today | after UoW-1, unfixed |
  |---|---|---|
  | claude ([base.py:488-492](../../octomate/tentacles/agent/claude/base.py#L488-L492)) | `live_clients[thread_id]`, and a new run **interrupts the previous one** for that thread | a commissioned claude **interrupts the user's own live claude run** in the same thread. Two concurrent commissions interrupt each other — **fan-out silently becomes serial-with-casualties** |
  | codex ([base.py:160-168](../../octomate/tentacles/agent/codex/base.py#L160-L168)) | `clients[thread_id]`, an LRU pool of warm app-servers | two concurrent commissions share one client across two different Codex threads |

  The fix is small and the intent is already written down — claude's own comment one line
  up says *"**One live run per conversation**"*, which is exactly right and exactly not
  what the code does. **Key both maps by `conversation_id`.** Do it in UoW-1, with the
  schema change that invalidates the assumption, not in UoW-3 where the symptom appears.
- **Transmuters** ([schemas/runs.py](../../octomate/schemas/runs.py),
  [schemas/conversation.py](../../octomate/schemas/conversation.py)): add the fields. No new
  polymorphic identity — **a child is not a variant, it is a relationship.** `kind` stays
  `octomate | external`; an octomate child and an external child are both real and must
  both be expressible. (The superseded plan offered "a dedicated `SubagentRun` polymorphic
  identity" as an alternative — it would force the two axes into one column.)
- **Manager** ([managers/conversation.py](../../octomate/managers/conversation.py)):
  `ensure(thread_id, *, agent_tentacle_id, subagent_id=None)`, threading through the
  `ConversationKey` cache tuple ([conversation.py:59-65](../../octomate/schemas/conversation.py#L59-L65))
  — it is a 2-tuple `(thread_id, agent_id)` today and must become a 3-tuple, or two
  subagents of the same agent in one thread collide **in the cache** while the database
  keeps them apart. `persist_run` is already generic over `RunT`
  ([:196](../../octomate/managers/conversation.py#L196)) and needs nothing.
- **Migration**: the `7a3e9c1b2f8d` shape — hand-written, `op.add_column` ×3 +
  `op.create_index`; SQLite adds columns in place, so `model_messages`' FKs are untouched.
  The `conversations` unique-constraint swap **does** rebuild that table on SQLite; do it in
  `batch_alter_table` and drop the old index before the rename (the lesson of
  `3f1c8ad42b91`).

**Acceptance:** a run can name a parent run and the tool call it answers; a thread holds one
bare conversation per agent (enforced, not assumed) and any number of child
conversations; a child's messages never appear in the parent agent's `conversation.messages`;
existing octomate + native paths pass untouched.

> ⚠ **SQLite FK enforcement is off in this repo** — the schema's `ON DELETE CASCADE` clauses
> are inert under test. The self-FK will not be enforced there either. Test the cascade
> behaviour explicitly rather than trusting the constraint.

## UoW-2 — routes that claim what they are

Today a route is `(agent_id, model, description)` and the description is one string per
**agent class**, so `codex/gpt-5.1-codex-mini` and `codex/gpt-5.5-pro` advertise identically
— a difference of roughly an order of magnitude in both cost and capability, invisible to
the model choosing between them.

**Agents advertise; the caller requests.** A route publishes the space it supports; the
caller picks a point in it.

```python
Effort: TypeAlias = Literal["low", "medium", "high"]   # normalized ACROSS agents
Cost:   TypeAlias = Literal["cheap", "standard", "premium"]

@dataclass(frozen=True)
class Claim:
    ability: str                   # what this route is for. per-route, not per-agent.
    efforts: tuple[Effort, ...]    # the effort levels it will accept
    cost: Cost                     # its baseline cost class

@dataclass(frozen=True)
class Route:                       # replaces SummonRoute
    agent_id: str
    model: AgentRouteModelName
    claim: Claim
```

- **Normalization is the whole point.** Codex speaks
  `none|minimal|low|medium|high|xhigh` ([config/agents.py:43](../../octomate/config/agents.py#L43)),
  inkling speaks `settings={"thinking": ...}`, Claude encodes it in the model name
  (`opusplan[1m]`). A caller must not have to know any of that. Each tentacle maps the
  normalized `Effort` onto its own knob; `Claim.efforts` says which ones it can honor.
- **Code defaults, config overrides.** The tentacle class ships a claim table for the models
  it knows; `octomate.yaml` overrides per route. A new model added in config inherits a
  documented default rather than claiming nothing.
- `scry` returns `list[Route]`; the gate instruction
  ([gate.py:40](../../octomate/capabilities/gate.py#L40)) grows a line on reading a claim.
  `summon` keeps its `Literal`-rewrite trick ([:96-103](../../octomate/capabilities/gate.py#L96-L103))
  — it exists to keep the ~500-entry `KnownModelName` union out of the tool schema, and
  `commission` inherits the same hazard.

**The open question of this UoW — settle it before writing code.** `AgentTentacle.run()` has
no `effort` parameter, and the generic knob that exists (`model_settings`) is **explicitly
ignored by claude and codex** ([claude/base.py:105-107](../../octomate/tentacles/agent/claude/base.py#L105-L107)).
Three ways:

| | cost | verdict |
|---|---|---|
| **(a) add `effort` to `AgentTentacle.run()`** | 2 overloads × 2 methods × 3 tentacles of churn | **settled — take it.** Effort becomes a first-class Octomate concept every tentacle honors, which is exactly what the ignored generic knob is not. The churn is mechanical, and **UoW-3 needs the same change for `subagent_id`** — one signature edit, two reasons. |
| (b) fold into `model_settings` | none | dead on arrival — claude and codex drop it |
| (c) make the route `(agent, model, effort)` and let config carry natives | no signature change | multiplies the route table; Codex's `effort` is one global config field, not per-route |

**Acceptance:** `scry` returns a per-route ability/effort/cost claim; two models of one agent
advertise differently; a caller-requested effort reaches the run on every tentacle, or is
refused as unsupported by that route's claim; an unclaimed config model gets its class default.

## UoW-3 — `commission`

The fourth spell. The spellbook's semantic gap, stated plainly:

| spell | who continues | whose thread | caller's turn |
|---|---|---|---|
| `summon` | another agent | takes it over, and the follow-ups | **over** |
| `teleport` | same agent | a new sub-thread | continues there |
| **`commission`** | **another agent** | **none — it has no surface** | **never pauses — the tool returns the result** |

A commission is a work order, and the agent that takes it is a temporary hand: it does the
one piece of work, reports back, and vanishes. It never owns the thread, never claims a
handoff, never appears in the ledger. That is the whole contrast with `summon`, and the
names have to carry it unaided — a model reaching for a subagent and hitting `summon`
instead hands the conversation away permanently.

**Where it lives.** `commission` is the gate's fourth spell, registered on the *same*
`FunctionToolset(id=GATE_TOOLSET_ID)` that `__post_init__` builds for `scry` / `summon` /
`teleport` ([gate.py:104-178](../../octomate/capabilities/gate.py#L104-L178)) — not a new
capability. Routing is one concern; splitting it across two capabilities would mean two
instruction blocks competing to explain one decision. Concretely:

- `COMMISSION_TOOL_NAME = "commission"` beside the existing three names
  ([gate.py:31-38](../../octomate/capabilities/gate.py#L31-L38)). **No `COMMISSION_KIND`** —
  a metadata kind exists to classify a *deferral* out of a finished run, and a commission
  never defers.
- `GATE_INSTRUCTION` ([gate.py:40](../../octomate/capabilities/gate.py#L40)) grows a
  `### commission` section. It already opens with plain words for what each opaque spell
  does — keep that bargain, and lead the new section with the `summon` contrast rather
  than the mechanism.
- It inherits `summon`'s `Literal`-rewrite ([gate.py:96-103](../../octomate/capabilities/gate.py#L96-L103),
  [:165-169](../../octomate/capabilities/gate.py#L165-L169)): stamp the live route
  `agent_id` / `model` literals onto the signature before registration, or the ~500-entry
  `KnownModelName` union drowns the real routes in the tool schema.
- `GateCapability` is constructed **fresh per `React` node execution**
  ([graph.py:504](../../octomate/reflex/graph.py#L504)) and is already stateful (`summon`
  mutates `decision`). A commission needs no new mutable field: unlike `summon`, it does
  not record a decision for the graph to read back — it returns its result inline.
- `allow_here` has no analogue. A commission has no destination: it never lands on a
  surface, so the group-main guard that `summon` needs simply does not apply.
- **It needs a handle on the agents.** `GateCapability` today holds only `routes` and
  `current_agent_id` — enough to *record* a choice. To *run* one it needs
  `dict[str, AgentTentacle]` and the `ConversationManager`. `React` already has both on
  `ctx.deps`; pass them in where it constructs the gate.

**Mechanism — an ordinary tool call. No deferral.**

`commission` is a plain `async def` whose body runs an agent and returns its output. It has a
twin, because **a commissioned agent is resumable** (§4a) — exactly as Claude's and Codex's
subagents are:

```python
async def commission(ctx, name, agent_id, model, effort, brief) -> str:
    """Put another agent to work. `name` is yours to choose — `commune` with it later."""
    child = await conversations.ensure(thread_id, agent_tentacle_id=agent_id, subagent_id=name)
    if child.runs:
        raise ModelRetry(f"{name!r} is already at work — `commune` with it, or pick a new name.")
    return (await agents[agent_id].run(brief, subagent_id=name, effort=effort, ...)).output

async def commune(ctx, name, message) -> str:
    """Speak again to an agent you commissioned. It remembers everything it did."""
    # resolves the SAME conversation -> the graph loads its history -> it continues
    return (await agents[resolved].run(message, subagent_id=name, ...)).output
```

**Two tools, not one with an optional `name`.** On a follow-up, `agent_id` / `model` /
`effort` are already settled — a single signature would carry three arguments that are
meaningless half the time. Distinct fields and behaviour ⇒ distinct variants (AGENTS.md). It
is also the shape both runtimes independently chose: `Agent` + `SendMessage`,
`spawn_agent` + `send_message`.

**The name is the identity.** `subagent_id = name`, scoped by `(thread_id,
agent_tentacle_id)` — precisely what UoW-1's unique index already enforces. Model-chosen and
mnemonic, so `commission` still returns a plain `str` with no handle to parse, and a person
reading the ledger sees `repo-audit` rather than `pyd_ai_01H8…`. A re-commission of a live
name is refused rather than silently continuing it; an unknown `commune` name raises a
`ModelRetry` **listing the live ones**, which is the same service a `list_agents` tool would
provide, at the only moment it is wanted.

**History comes for free, and this is §4a paying off.** `commune` resolves the *same* child
conversation, and a conversation **is** the history — every react node reads
`conversation.messages` ([react.py:116-118](../../octomate/capabilities/react.py#L116-L118)).
Nothing is replayed by hand. For a commissioned claude/codex, the child conversation's
`external_id` is its native session, so `resume=` continues it the same way. Had the
conversation been keyed on `parent_run_id`, none of this would work — a follow-up in a later
parent turn would have landed in a *different* conversation with no memory.

> **This is what settles UoW-2's open question.** `run()` must now carry **two** new
> arguments — `effort` (UoW-2) and `subagent_id` (here) — and neither can travel in
> `model_settings`, which claude and codex drop on the floor. One signature change, two
> reasons: take option (a).

**Fan-out is free.** pydantic-ai executes a response's tool calls concurrently by default —
`_parallel_execution_mode_ctx_var` defaults to `'parallel'`
(`pydantic_ai/tool_manager.py:40-41`) and each call becomes its own `asyncio.create_task`
(`pydantic_ai/_agent_graph.py:1915-1925`). Three `commission` calls in one response are
three concurrent child runs, with no machinery of ours. **Do not register it
`sequential=True`** — one sequential tool forces the *entire* batch sequential
(`tool_manager.py:162-164`).

**Both ids the run tree needs are already in hand:** `ctx.run_id`
(`pydantic_ai/_run_context.py:86`) is the parent run's PK, and `ctx.tool_call_id` (`:62`) is
the call this child answers. They are exactly `parent_run_id` and `parent_tool_call_id`.

> **An earlier draft of this plan deferred the commission** through a `CallDeferred` + a
> batch + a `Commission` graph node, on the theory that "background" required it. That was
> wrong, and the record is kept here so it is not re-proposed. A deferral ends the parent
> run, so it buys exactly one thing an awaited tool call does not: **durability across a
> restart** — which we deliberately do not want (below). It bought nothing and cost a third
> `DeferredAction` variant, a graph node, batch persistence, and a suspender branch.
> `teleport`'s deferral is *not* the precedent: teleport defers because it must **relocate
> the run itself**, which a tool cannot do from inside. A commission stays put.

- **Only inkling can commission.** Claude and codex ignore injected capabilities
  ([claude/base.py:105-107](../../octomate/tentacles/agent/claude/base.py#L105-L107)) —
  they cannot even `summon` today. They do not need to: they have native subagents, and
  UoW-4/5/6 records those. State this; do not try to fix it here.
- **Rendering is out of scope.** The child's events do not stream to the channel in this
  UoW; the durable child run is enough. `StreamBlockType`'s unused `"subagent"` block
  ([feelers/output.py:152](../../octomate/tentacles/channel/feelers/output.py#L152)) is
  where that lands later.

### The guards — what a commissioned run is *not* given

An awaited tool call makes these load-bearing rather than hygiene: every level of nesting
holds a **live** parent run parked in a **live** tool call. Nothing unwinds on its own.

One principle settles all of them: **a commission has no thread of its own.** It borrows the
parent's surface for approvals and for nothing else. Each guard below is that sentence
applied.

| the child is not given | because | what would happen otherwise |
|---|---|---|
| **`summon`** | it acts on a thread surface; a commission has none | the child hands away a conversation it does not own |
| **`teleport`** | same | the child relocates a run nobody is reading |
| **`ask_questions`** ([inkling/tools.py:12](../../octomate/tentacles/agent/inkling/tools.py#L12)) | **there is no user to ask** | ⚠ see below — this one *deadlocks* |
| **a `deferred_suspender`** | the same reason | a batch is persisted and presented that nothing will ever resume |
| **`commission`, past the depth cap** | bounded recursion | inkling → inkling → … , live at every level |

⚠ **The deadlock is the sharp one, and it is silent.** A commissioned inkling that calls
`ask_questions` raises `CallDeferred`; its run ends with `DeferredToolRequests`; the
suspender persists a batch and presents a card. A human answers it. `Octomate.kick` routes
the response to the **reflex graph** — which knows nothing of a run being awaited inside
someone's tool call. The parent stays parked forever, and the only symptom is a turn that
never comes back. **Give a commissioned run no suspender and no ask tool**, and let a
deferral it produces anyway surface as a tool failure the parent can see.

**Approvals still work, and must.** A commissioned claude/codex needs permission to edit
files, and `in_process` agents park a future in `pending` that `Octomate.kick` resolves
directly ([base.py:142-155](../../octomate/base.py#L142-L155)) — no graph involved, so it
resolves cleanly while the parent awaits. That is the one thing a commission legitimately
borrows the parent's surface for. It is also why "no user" is stated as "no thread": the
distinction is load-bearing.

**Depth.** The child's gate is constructed by the commission tool itself (it builds the
child's `capabilities=`), so depth is simply carried on `GateCapability` and incremented per
level. **At the cap, do not offer the tool** — drop `commission` from the child's toolset
rather than registering it and raising `ModelRetry`. A model cannot misuse a tool it never
sees, and the retry budget is not spent teaching it a rule the schema could have stated.
Self-commission is refused as `summon` already refuses it
([gate.py:150](../../octomate/capabilities/gate.py#L150)); note this is **weaker than
summon's** guard, because A → B → A is a legal cycle that self-check alone does not catch —
the depth cap is what actually bounds it.

**Timeout.** A commissioned claude can run for many minutes holding the parent's tool call
open. Bound it, and surface the expiry as a tool failure. `approval_timeout` is the existing
precedent for "a wait that must not be unbounded"
([config/agents.py:113](../../octomate/config/agents.py#L113)).

**Restart is fail-fast, and now for free.** A commission in flight when Octomate dies dies
with it — the parent run was in memory, and there is nothing to resume. No cleanup pass, no
`failed` marking, no batch to reconcile. This is what we wanted anyway (a child may have
already edited files; re-running is not idempotent), and it is the clearest argument that
the deferral was buying nothing: its one advantage was a durability we would have had to
write code to *refuse*.

**Acceptance:** inkling commissions codex and its turn continues with the child's output as
the tool result; the child's run carries `parent_run_id` + `parent_tool_call_id` and lives in
its own conversation; three commissions in one response run concurrently **and all three
complete** (the live-client fix, UoW-1); the child's timeline never enters inkling's history.

**Resumption:** `commune` with a commissioned agent and it answers **from its own history** —
one conversation, two runs, the second's `parent_run_id` naming the later parent turn; a
`commune` in a *later* parent turn still reaches it; re-commissioning a live name is refused;
an unknown name lists the live ones; a commissioned claude resumes its native session rather
than starting a fresh one.

**Guards:** a commissioned run is offered neither `summon`, `teleport`, nor `ask_questions`,
and past the depth cap not `commission`/`commune` either; a commissioned inkling that defers
anyway **fails loudly rather than hanging**; a commissioned claude's approval card still
reaches the human and unblocks the child; self-commission is refused; an over-running
commission fails the tool rather than hanging the turn.

## UoW-4 — native subagent transcripts (Claude) ✅ shipped (`7241206`)

Delete the lineage-reconstruction idea. Watch the directory.

1. **Discovery.** A session's subagents live in `<transcript dir>/<session-id>/subagents/`.
   The tailer already watches the transcript's **parent directory**; the subagent dir is a
   sibling of the transcript file, named for its stem. Watch it too; each `agent-*.jsonl` is
   a transcript in its own right.
2. **One tail per file, reusing everything.** A subagent file is fed to the same
   `ClaudeRunAccumulator`, framed on `\n`, cursored by byte offset, committed via
   `record_external_run` — the same translation, the same idempotency, the same sink. The
   *only* differences are turn framing (below) and that a child **never binds the ledger**
   (`bind_ledger`, [tailer.py:512](../../octomate/tentacles/agent/claude/tailer.py#L512)):
   there is no human prompt and no human answer in a subagent.
3. **Turn framing is different, and this is the sharp edge.** `promptSource` — the
   marker the parent's framing keys on ([tailer.py:411](../../octomate/tentacles/agent/claude/tailer.py#L411))
   — **is absent from subagent files entirely**. Not null: absent. And a subagent file is
   *not* one turn: **4 of 93** in the corpus carry more than one `promptId` (up to 3), from
   subagents resumed after their first result. So:
   **frame a subagent turn on a change of `promptId`.** It is the only marker present, and
   it is exactly what the parent's turn key already is.
4. **The child run's id.** `promptId` on a subagent line is the **parent turn's** id — using
   it as the child's PK collides with the parent run. Key the child
   `f"{agentId}:{promptId}"`, which is exactly *(which subagent, which parent turn)* per
   §4a: unique, stable, derivable from the file alone. `agentId` also becomes the child
   **conversation's** `subagent_id` + `external_session_id`, and `promptId` its run's
   `parent_run_id` — so a subagent resumed by a later parent turn adds a **second run to the
   same conversation**, never a second conversation.
5. **Sequencing.** A child run needs its `parent_run_id` — so the parent run must exist
   before the child commits. But a parent turn only closes at the *next* prompt, which may
   be minutes after its subagent finished. **The sketch is the answer** (§UoW-5): the hooks
   write the parent run at `UserPromptSubmit`, so by the time any subagent exists, its
   parent run does. Without UoW-5, UoW-4 must hold child commits until the parent turn
   closes — do not do that; ship them together or ship UoW-5 first.
6. **`recover()` must not mix files.** `max(end_offset)` is per conversation, and children
   are in their own conversation — so this is already correct *by construction* under UoW-1.
   **Assert it in a test anyway.** It is the single most expensive thing to get wrong: a
   mixed `max()` strands turns where no recovery can reach them, silently.

**Acceptance:** a native session that spawns three subagents records the parent run and three
child runs, each with its own byte range into its own file, each linked by `parent_run_id` +
`parent_tool_call_id`; a resumed subagent records two child runs, not one; re-tailing the
same session reproduces the identical tree; a session with no subagents is byte-for-byte
unchanged; a pre-2.1.177 transcript with inline sidechain lines still ingests its parent
turns cleanly.

## UoW-5 — native subagent hooks (Claude) ✅ shipped (`56c1c5b`, settle fix `5ac54e0`)

The live tier, and it maps one-to-one onto the tier that already works:

| parent turn | subagent | carries |
|---|---|---|
| `UserPromptSubmit` | **`SubagentStart`** | `agent_id`, `agent_type`, `prompt`, `transcript_path` |
| `Stop` | **`SubagentStop`** | `agent_id`, `agent_type`, `last_assistant_message` |

Both are HTTP-deliverable. Add them to `HANDLED_HOOK_EVENTS`
([hooks.py:18](../../octomate/tentacles/agent/claude/hooks.py#L18)) and to `ClaudeHookInput`
as `agent_id` / `agent_type` (the envelope is `extra="ignore"`, so one model still validates
every event). `SubagentStart` starts the child's tail; `SubagentStop` closes it.

- **`agent_id` is the discriminator.** It appears on *any* hook fired inside a subagent —
  that is how a subagent's `PreToolUse` is told from the parent's. We handle neither, but
  the rule matters: **an event carrying `agent_id` is not a parent-turn event.** Without
  that guard a `Stop` from inside a subagent would close the parent's turn.
- **The child sketch.** Same reasoning as the parent's ([native-session-ingest.md §5](done/native-session-ingest.md)):
  `SubagentStart` writes a provisional child run so a subagent in flight has somewhere to
  hang; the tailer supersedes it with the full timeline. **Date it** — an undated run sorts
  ahead of the history it belongs at the end of.
- **`driving` still suppresses.** A subagent of a session Octomate drives is still Octomate's
  own work; the existing session claim ([ingest.py:100](../../octomate/tentacles/agent/claude/ingest.py#L100))
  keys on `session_id`, and a subagent event carries the **parent's** `session_id` — so it is
  suppressed for free. Confirm with a test rather than trusting this sentence.

**Verified live (see §6):** both events reach the http handler; `prompt_id` is absent
from `SubagentStart`, so the sketch path is dormant and the graceful degradation below is
the real path; the settle race in §6 finding 2 was found and fixed here. Originally
flagged for empirical verification — the ingest design has been burned before
("SessionStart is delivered to command hooks only" was learned, not read):

1. **Is `transcript_path` on `SubagentStart` the subagent's own file or the parent's?** The
   docs show `/path/to/transcript.jsonl` and do not say. If it is the child's, UoW-4 needs no
   directory discovery at all — the hook hands over the exact file. If it is the parent's,
   derive the child's from `<stem>/subagents/agent-<agent_id>.jsonl`. **Both must still pass
   the transcript-root sandbox** ([ingest.py:183](../../octomate/tentacles/agent/claude/ingest.py#L183)):
   the path is the caller's claim, and the subagent dir is *inside* the accepted tree, so no
   root widening is needed — verify that, do not assume it.
2. **Do `SubagentStart`/`SubagentStop` actually reach an `http` handler?** The docs say every
   event supports HTTP. The docs also imply that of `SessionStart`, which does not. Register
   them, fire a real subagent, and look.

Also unverified: **whether `SubagentStart` fires again when a subagent is resumed.** If it
does not, the 4-of-93 multi-turn files have no live signal for their later turns, and only
the tailer sees them. That is acceptable (the tailer is the complete tier) but must be known.

**Acceptance:** a native subagent's prompt and final answer are recorded live, before its
transcript closes; the child run exists from `SubagentStart` and is superseded, not
duplicated, when the tailer commits; a `Stop` carrying `agent_id` never closes the parent's
turn; an Octomate-driven session's subagents are not ingested.

## UoW-6 — Codex subagents as child runs — **assigned to Codex; read §7 first**

Ships last, as agreed. But it is **not** the small gap an earlier draft of this plan called
it — that draft (and the code it trusted) had Codex's model exactly backwards. §5 has the
corrected shape; this is the work.

**Step 0 is a deletion.** Remove the nesting branch and `nested_turn_ids`
([codex/tailer.py:216-245](../../octomate/tentacles/agent/codex/tailer.py#L216-L245), `:83`).
It detects a thing that does not happen and, per §5, is the mechanism of a live 24 % data
loss. Its test, `test_nested_task_does_not_close_or_pollute_the_parent`
(`tests/agent/test_codex_native_ingest.py:218`), pins the fiction — it must be **deleted, not
adapted**. A truly nested `task_started` in one rollout means an *interrupt*, not a subagent.

Then the actual capture:

1. **Discover by content, never by path.** A subagent rollout is
   `sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl` — **byte-identical in shape and
   location to a normal session**. `session_meta` is the only discriminator. Key on
   **`source.subagent`**, not the top-level `parent_thread_id`: rollouts written before
   ~v0.137 carry only the former, so `source.subagent` is the durable field and
   `parent_thread_id` the convenient one.
2. **Filter to `thread_spawn`, or drown.** Of 229 local rollouts: **110 are
   `subagent | guardian`** — Codex's internal approvals reviewer — and only **15** are real
   user-facing `thread_spawn` delegations. `SubAgentSourceValue` is
   `review | compact | memory_consolidation` plus free-form `other` (where `guardian`
   lives). Ingesting `thread_source == "subagent"` naively floods the ledger with 110
   internal threads nobody asked for. **This filter is the whole difference between the
   feature and a mess.**
3. **Join to the parent.** `session_meta.source.subagent.thread_spawn.parent_thread_id` is
   the parent thread; `session_id` is shared across the entire session tree (SDK: *"Session
   id shared by threads that belong to the same session tree"*), so it identifies the tree,
   **not** the thread — do not key a conversation on it. `depth` and `agent_path`
   (`/root/final_di_review`) come free in the same payload; `agent_path` is a literal tree
   path and is the cheapest depth check we will ever get.
4. **`parent_tool_call_id` comes from an event we do not model.** The parent's only trace of
   its subagent is `event_msg` / **`sub_agent_activity`**, whose `event_id` **is the spawning
   tool call id**:
   ```json
   {"type":"sub_agent_activity","event_id":"call_47upaDUcyvr93umTPg4hFU8t",
    "agent_thread_id":"019f6b28-3ad5-78b0-979f-70a0f1661b0b",
    "agent_path":"/root/logfire_check","kind":"started"}
   ```
   `event_id` → `parent_tool_call_id`, `agent_thread_id` → the child's rollout. This is
   Codex's exact analogue of Claude's `toolUseResult.agentId`, and it is the *only* source.
5. **Dedup fork replay — the trap that silently doubles everything.** `spawn_agent` is
   called with `"fork_turns": "all"`, so a subagent's rollout **replays its parent's entire
   turn history verbatim, carrying the parent's `turn_id`s**, before its own turn begins. In
   the one local parent/child pair, **5 turn_ids appear in both files and 1 is genuinely the
   child's.** Tailing both without a guard records five parent turns twice.
   `record_external_run`'s idempotency does **not** save us: it keys on run id, and a
   replayed turn has the *same* `turn_id` — so the child's copy is silently dropped, which
   is right by luck, not by design. What actually breaks: the replay leaves the parent's
   in-flight turn **unclosed** in the child file. Ingest only turns whose `turn_id` shares
   the child thread's UUIDv7 prefix (thread `019f59af-5af6…` → turn `019f59af-5bb7…`), or
   skip every line before the child's own first `task_started`.
6. **Model the missing line kinds.** `compacted` (140 local) and
   `inter_agent_communication_metadata` (97) are absent from the `RolloutLine` union
   ([codex/transcript.py:25](../../octomate/tentacles/agent/codex/transcript.py#L25)) and are
   silently dropped at [tailer.py:202-205](../../octomate/tentacles/agent/codex/tailer.py#L202-L205).
   Harmless today; name them so the next reader knows they were seen and declined.

**Hooks** (`SubagentStart` / `SubagentStop`) mirror UoW-5 with two Codex specifics:
`SubagentStop` carries **`agent_transcript_path`** — the exact file, no discovery needed, and
strictly better than Claude's ambiguous `transcript_path` — and it *"expects JSON on stdout
when it exits 0; plain text output is invalid for this event."* Our `emit.py` returns `{}` for
every event already, so this costs nothing. `session_id` is the **parent's**, exactly as in
Claude, so `agent_id` is again the discriminator. We handle 3 of Codex's 10 hook events.

> **Unverified — settle before building on it.** `agent_transcript_path` is documented and in
> the binary's JSON schema, but **was never observed live** on this machine (the local
> `hooks.json` does not subscribe to `SubagentStop`). Neither is it confirmed that the hooks'
> `agent_id` equals the rollout's `thread_id` — the docs expose no `thread_id` in a subagent
> hook payload. **Subscribe, spawn one real subagent, and look**, before designing the join
> on either. If the bridge does not hold, the fallback is discovery-by-`session_meta`, which
> needs no hook at all.

> **Not available at any price:** the subagent's task prompt. `spawn_agent`'s `message` arg is
> an encrypted blob (`gAAAAAB…`) in the rollout; only `{"task_name": …, "fork_turns": "all"}`
> is plaintext. A Codex child run's opening prompt comes from `SubagentStart`, or not at all
> — unlike Claude, where the subagent's first transcript line *is* the prompt.

**Acceptance:** a Codex rollout tree records the parent run and its `thread_spawn` subagents
as child runs, linked by `parent_run_id` + `parent_tool_call_id` (from `sub_agent_activity`);
guardian threads are **not** ingested; fork-replayed parent turns are not double-recorded;
`nested_turn_ids` and its test are gone; a rollout with no subagent is unchanged.

---

## Risks

| risk | why it bites | mitigation |
|---|---|---|
| **The NULL-in-unique-constraint trap** (UoW-1) | A *nullable* `subagent_id` under a three-column unique constraint silently stops enforcing one-conversation-per-agent — NULLs are distinct, duplicates accumulate until a cache returns the wrong history. | The `""` sentinel (the `Thread.thread_id` convention): empty is a value, so one plain constraint enforces both halves. Test that a second bare conversation for the same (thread, agent) **raises**. |
| **Live clients keyed by `thread_id`** (UoW-1) | Claude interrupts "the previous run for the same thread" — so a commissioned claude kills the user's live run, and two concurrent commissions kill each other. **Fan-out degrades to serial with casualties, silently.** The assumption is written in the comment; UoW-1 is what falsifies it. | Re-key `live_clients` / `clients` to `conversation_id` **in UoW-1**. Test: two concurrent commissions to one agent in one thread both complete. |
| **Keying the child conversation on `parent_run_id`** (UoW-1) | A subagent resumed by a later parent turn (§4a — measured, 4 of 93) has runs with **different** `parent_run_id`s. Keying the conversation on it splits one continuous context in two, and the second half reads as an agent with amnesia about work it visibly did. | The conversation is keyed by the subagent's own id; `parent_run_id` lives on the **run**. Test that a two-turn subagent yields **one** conversation with two runs. |
| **Child offsets in a parent's conversation** (UoW-4) | `max(end_offset)` spans two files, resumes at a nonsense byte, strands turns where recovery cannot reach them — the exact failure invariant 4 was written for. | Structural: children get their own conversation. Plus an explicit test, because the failure is silent. |
| **`promptId` reused as the child's PK** (UoW-4) | Subagent lines carry the *parent's* `promptId`; using it collides with the parent run and `record_external_run` returns `None` — a **silent** skip, since that is its idempotency signal. | Key children `agentId:promptId`. Test that a subagent's run id differs from its parent's. |
| **Codex's guardian flood** (UoW-6) | 110 of 229 local rollouts are the internal approvals reviewer, not user delegation. A naive `thread_source == "subagent"` filter ingests all of them — the ledger fills with threads nobody asked for and the real 15 are lost in them. | Filter to `source.subagent.thread_spawn`. Test that a `guardian` rollout is **not** ingested. |
| **Codex fork replay** (UoW-6) | `spawn_agent` uses `"fork_turns": "all"`, so a child's rollout replays the parent's whole history **carrying the parent's `turn_id`s**. Run-id idempotency drops the duplicates by luck, not design — but the replay also leaves the parent's in-flight turn unclosed in the child file, which the old nesting branch would then cascade on. | Ingest only turns whose `turn_id` shares the child thread's uuid7 prefix, or skip everything before the child's own first `task_started`. |
| **`.meta.json` is undocumented** (UoW-4) | The most convenient linkage is the least supported; it can vanish in a patch release. | Correctness rests on the documented `promptId` + `toolUseResult.agentId`. Read the sidecar for `agentType`/`spawnDepth` only, and tolerate its absence (`spawnDepth` is already missing on 59 of 93 local files). |
| **A subagent `Stop` closing the parent's turn** (UoW-5) | Would truncate the parent's byte range mid-turn and commit a partial run — which, carrying an `end_offset`, is then **final and idempotent**. Unrecoverable without manual deletion. | `agent_id` present ⇒ not a parent event. Guard at the dispatch, not per handler. |
| **Unbounded commission recursion** (UoW-3) | Unlike `summon`, the caller stays live at every level — an awaited tool call parked in an awaited tool call. Nothing unwinds on its own. | Depth on `GateCapability`, incremented per level; **at the cap the tool is not offered at all**. Self-commission refused — but note A → B → A defeats the self-check, so the cap is the real bound. |
| **`effort` cannot reach claude/codex** (UoW-2) | They ignore `model_settings` — the obvious channel is a no-op, and a silently-ignored effort is worse than an unsupported one. | Option (a): a real `effort` kwarg on `run()`. Refuse an effort outside the route's claim rather than dropping it. |
| **A commissioned inkling asks a question** (UoW-3) | **Silent deadlock.** The child defers, a card is presented, a human answers, and `kick` routes the response to the reflex graph — which knows nothing of a run awaited inside a tool call. The parent parks forever; the only symptom is a turn that never returns. | Give a commissioned run no suspender and no `ask_questions`. Surface any deferral it produces anyway as a tool failure. Test that a commissioned run which defers **fails** rather than hangs. |
| **A commission never returns** (UoW-3) | It holds the parent's tool call, and the parent's turn, open indefinitely. | Bound it; expiry is a tool failure. `approval_timeout` is the precedent. |

## Non-goals

- **Streaming a child's events to the channel.** The durable child run is the deliverable;
  `StreamBlockType`'s `"subagent"` block is where live fan-out rendering lands later.
- **Commissioning from claude or codex.** They ignore capabilities and have native subagents.
- **Nested native subagents.** Claude's `spawnDepth` is 1 in every local sample where present, and Codex's `agents.max_depth` **defaults to 1** — a grandchild is off by default on both. The model represents depth; the ingest need not chase a case the runtimes do not produce.
- **Codex's non-delegation subagents** — `guardian`, `review`, `compact`, `memory_consolidation`. They are the runtime talking to itself, not work a human asked for.
- **Reading a Codex subagent's prompt from the rollout.** It is encrypted. `SubagentStart` or nothing.
- **Re-running a failed commission.** Recovery stays an explicit act, as it is for the tailer.
- **Retro-ingesting pre-2.1.177 inline-sidechain transcripts.** The `is_sidechain` guard
  keeps them parsing; their subagents stay dropped. No such transcript exists locally.

## Where to read

| concern | file |
|---|---|
| the spellbook `commission` joins | [capabilities/gate.py](../../octomate/capabilities/gate.py) |
| why a commission is *not* shaped like `teleport` | `TeleportRequest` in [reflex/suspender.py](../../octomate/reflex/suspender.py), `Teleport` in [reflex/graph.py](../../octomate/reflex/graph.py) |
| the deferral a commissioned run must never reach | `HumanReviewSuspender` in [reflex/suspender.py](../../octomate/reflex/suspender.py), `ask_questions` in [inkling/tools.py](../../octomate/tentacles/agent/inkling/tools.py) |
| in-process approval resolution (why claude's cards still work) | `kick` in [base.py](../../octomate/base.py) |
| tool-call concurrency + `RunContext` ids (the two facts UoW-3 rests on) | `pydantic_ai/tool_manager.py`, `pydantic_ai/_run_context.py` |
| the run + conversation model to extend | [models/runs.py](../../octomate/models/runs.py), [models/conversation.py](../../octomate/models/conversation.py) |
| the durable sink and its idempotency rule | `record_external_run` in [managers/conversation.py](../../octomate/managers/conversation.py) |
| the skip that stops being load-bearing | [claude/tailer.py:402-420](../../octomate/tentacles/agent/claude/tailer.py#L402-L420) |
| turn framing, commit, recover | [claude/tailer.py](../../octomate/tentacles/agent/claude/tailer.py) |
| hook payload model + event list | [claude/hooks.py](../../octomate/tentacles/agent/claude/hooks.py) |
| the sandbox any subagent path must pass | `transcript_roots` in [claude/ingest.py](../../octomate/tentacles/agent/claude/ingest.py) |
| the false comment + the branch to delete | [codex/tailer.py:216-245](../../octomate/tentacles/agent/codex/tailer.py#L216-L245) |
| the live abort bug (§1b) | `task_complete` in [codex/tailer.py:260-266](../../octomate/tentacles/agent/codex/tailer.py#L260-L266) |
| the rollout line kinds we model — and the two we do not | [codex/transcript.py:25](../../octomate/tentacles/agent/codex/transcript.py#L25) |
| Codex's 10 hook events, of which we handle 3 | [codex/hooks.py](../../octomate/tentacles/agent/codex/hooks.py), `HookEventName` in `openai_codex/generated/v2_all.py:1466-1476` |
| the ingest design this extends | [native-session-ingest.md](done/native-session-ingest.md) |
| the migration shape to copy | `migrations/versions/2026_07_15_1530-7a3e9c1b2f8d_external_agent_run_variant.py` |

The tests are the specification: each one names the failure it prevents.
