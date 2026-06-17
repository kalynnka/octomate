# Plan: remove the triage graph → dispatch straight to the react graph + minimal in-channel `switch_to_thread`

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-16
> **Minimal subset of:** [channel-retargeting.md](channel-retargeting.md) (full M2:
> cross-channel sticky re-home). This ships **only** the triage collapse and an
> **in-channel** switch (move into a sub-thread of the *current* chat). It
> deliberately **drops** cross-channel / cross-platform targeting, candidate sets,
> and the `InklingDeps` prerequisite — none are needed for an in-channel switch.

## TL;DR

Two moves, one PR:

1. **Delete the triage graph.** Remove `triage_graph` and all its nodes
   (`Awake → Route → RunTriage → PrepareReception → RunReception → ResumeDeferred`)
   entirely. `Octomate.kick` instead **drives the react graph directly** via the
   inkling tentacle's existing `run` / `run_stream_events`
   ([base.py:128-178](../../octomate/tentacles/agent/inkling/base.py#L128-L178)),
   handling source-context resolution, delivery, and human deferred-resume inline.
   No pre-model triage *screen*, no second graph layer — the agent decides
   everything.
2. **Minimal in-channel switch.** A new deferred tool `switch_to_thread(hint)` lets
   the agent say "let's continue this in a thread." Dispatch forks a sub-thread
   **of the current chat on the same channel**, clones the run's history into it,
   and re-drives the react graph there — "two runs, seamless from the outside."

The destination is always "a thread here," so the tool takes **no target**, needs
**no deps**, and touches **no identity** — it's `RunContext[None]`, like
`ask_questions` ([tools.py:11-19](../../octomate/tentacles/agent/inkling/tools.py#L11-L19)).

## Scope boundary

**In scope — the only switch destination:** a new sub-thread of the *current*
chat, available only when the current channel's `thread_strategy != "main_only"`
(Slack + Lark `flat_thread`; [channel/base.py:47](../../octomate/tentacles/channel/base.py#L47),
`:131`). On `main_only` channels (Napcat, Web) the tool is a no-op fallback (stay
put) — see §4.

**Explicitly out of scope (deferred to full M2):** switching to *another* channel,
cross-*platform* switch (needs the parked identity map), opening a brand-new DM,
and any candidate/`target_id` selection. Because there's exactly one possible
destination, none of that machinery is built.

## Current shape (what we're collapsing)

`Octomate.kick` runs `triage_graph` ([base.py:64-88](../../octomate/base.py#L64-L88)):
`Awake → Route → RunTriage → PrepareReception → RunReception` (+ `ResumeDeferred`
for human deferred-resume), [graph/triage.py](../../octomate/tentacles/agent/inkling/graph/triage.py).
`RunTriage` runs the inkling agent with `output_type=[TriageDecision, …]` and
`TRIAGE_INSTRUCTIONS` to pick answer-vs-reception + a cross-channel `target_id`;
`RunReception` runs the full agent and delivers (stream or present). The graph
result is discarded by `kick` — delivery happens inside the nodes, so collapsing
breaks no downstream consumer.

## Design / units of work

### 1. `switch_to_thread` tool + `SwitchCapability`

New `octomate/capabilities/switch.py`, following the Send/Todo capability pattern:

```python
SWITCH_TOOL_NAME = "switch_to_thread"   # add to octomate/constants.py

def build_switch_toolset() -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool(name=SWITCH_TOOL_NAME)
    async def switch_to_thread(ctx: RunContext[None], hint: str) -> str:
        """Continue this conversation in a new thread off the current chat.
        `hint` is the short user-facing thread-starter message."""
        raise CallDeferred(metadata={"kind": "switch", "hint": hint})

    return toolset

class SwitchCapability(AbstractCapability[None]):
    def get_toolset(self) -> AbstractToolset[None] | None: ...
    def get_instructions(self) -> AgentInstructions[None] | None:
        # when to move to a thread (multi-step/tool-heavy/long-running work),
        # and that it only works as a sub-thread of the current chat.
```

Register in [main.py:78-82](../../main.py#L78-L82) alongside `SendCapability`.
No `InklingDeps`, no candidate block, no `target` arg.

### 2. `ConversationManager.clone_into(source, target)`

New method (mirrors `record_agent_run`'s `vars(m)` re-blessing,
[managers/conversations.py:105-131](../../octomate/managers/conversations.py#L105-L131)):
copy the source conversation's full message history into the target conversation
(created via `ensure`) as one cloned `AgentRun`, then `refresh` the target.

- **Must preserve the trailing deferred `ModelResponse`** (the `switch_to_thread`
  call) so the resume against the clone is valid — i.e. do *not* drop the trailing
  deferral here (unlike `StartTurn`, [react.py:108-115](../../octomate/capabilities/react.py#L108-L115)).
- Copy this conversation's todo rows too (TodoManager is keyed by
  `conversation_id`), so the in-flight task list survives the switch. Memory keys
  derive from `thread_id` and are intentionally *not* carried.

### 3. Delete the triage graph; dispatch from `kick` straight to react

**Delete** [graph/triage.py](../../octomate/tentacles/agent/inkling/graph/triage.py)
in full — `triage_graph`, `TriageState`, `TriageDeps`, and every node (`Awake`,
`Route`, `RunTriage`, `PrepareReception`, `RunReception`, `ResumeDeferred`) — plus
its re-exports from `graph/__init__.py` and the imports in
[base.py:21-26](../../octomate/base.py#L21-L26). Drop `TRIAGE_INSTRUCTIONS`, the
`TriageDecision` model run, cross-channel candidate building, and `target_id`
routing.

**Replace** the `triage_graph.run(...)` call in `Octomate.kick`
([base.py:64-88](../../octomate/base.py#L64-L88)) with a plain dispatch (kept in
`kick`, or a small `octomate/tentacles/agent/inkling/dispatch.py` helper it calls).
`kick` already has `channels`, `conversations`, `deferred_actions`, `agents`, so it
needs no graph state/deps object. Two branches, mirroring today's signal split:

- **`UserMessageSignal`** (the old `Awake → … → RunReception` happy path):
  1. Resolve context as `Awake` did
     ([triage.py:146-168](../../octomate/tentacles/agent/inkling/graph/triage.py#L146-L168)):
     channel, `agent_id = channel.config.agent_id`, `ensure()` the conversation,
     build `user_prompt`; keep the empty-signal / empty-prompt short-circuits.
  2. Build a `HumanReviewSuspender` for the conversation key (unchanged wiring,
     §4 makes it switch-aware).
  3. **Drive the react graph directly** via the inkling tentacle and deliver,
     lifting `RunReception`'s stream-vs-present logic
     ([triage.py:443-498](../../octomate/tentacles/agent/inkling/graph/triage.py#L443-L498)):
     stream channels → `async with channel.feelers.timeline.open(key)` + `drive(agent.run_stream_events(...))`;
     non-stream → `result = await agent.run(...)` then `present` str / `segments` list.
     `run`/`run_stream_events` already run the react graph internally — that's the
     "directly call the react graph."
  4. On the run's `DeferredToolRequests`: a `switch_to_thread` call → the **switch
     sequence** below; questions/approvals were already persisted by the suspender
     → just return.
- **`DeferredActionBatchResponse`** (the old `ResumeDeferred` re-entry,
  [triage.py:530-617](../../octomate/tentacles/agent/inkling/graph/triage.py#L530-L617)):
  resolve the batch via `DeferredActionManager`, rebuild `deferred_tool_results`,
  drive react `run(deferred_tool_results=…, conversation_key=batch.target_key)`,
  deliver, mark the batch completed. This is durable re-entry **without** a graph —
  the second `kick` + the persisted batch are the continuation.

**Switch sequence** (inline in dispatch, was M2's `Switch` node):
  a. Current channel `main_only` **or** already in a thread → resolve the call as
     "staying here," re-drive react in the **current** conversation (no fork).
  b. Else `target_key = await channel.start_sub_thread(current_key, hint)` (reuse
     `PrepareReception`'s try/except fallback-to-main,
     [triage.py:371-388](../../octomate/tentacles/agent/inkling/graph/triage.py#L371-L388));
     on failure → (a).
  c. `ensure()` the target conversation; `clone_into(current → target)` (§2).
  d. Build `DeferredToolResults` resolving the `switch_to_thread` call.
  e. Re-drive react with `conversation_key = target_key` + the deferred results →
     run2 resumes via `ResumeTurn → RunAgent`
     ([react.py:119-137](../../octomate/capabilities/react.py#L119-L137)) against the
     cloned history (which holds the pending call), delivering to the thread.

**`TriageDecision` persistence:** batches still store a `decision` field
([suspender.py:34](../../octomate/tentacles/agent/inkling/graph/suspender.py#L34));
dispatch passes `decision=None` (the suspender already accepts `None`). Ripping the
field out of the deferred-batch schema is a separate cleanup, not this PR.

> **Why no graph is fine:** the only reason M2 kept a thin graph was durable
> re-entry for human deferred resume. But that re-entry is already a fresh `kick`
> with a `DeferredActionBatchResponse` reading a *persisted* batch — `kick` is the
> durable boundary, not the graph. So the triage graph adds a layer without adding
> durability; removing it loses nothing. (The **react** graph stays — it's what
> `run`/`run_stream_events` drive.)

### 4. Suspender switch-awareness

`HumanReviewSuspender.suspend` ([graph/suspender.py:38-85](../../octomate/tentacles/agent/inkling/graph/suspender.py#L38-L85))
must **not** persist/present a batch when the requests are a `switch_to_thread`
call (detect by tool name): return `None` so run1 ends cleanly and the switch
bubbles to `Dispatch` for resolution. Questions/approvals unchanged. (Assume a run
that switches doesn't also `ask_questions` — guard/treat switch as exclusive.)

## Verification

- **Unit:**
  - `switch_to_thread` raises `CallDeferred` with `{"kind":"switch","hint":…}`;
    suspender returns `None` (no batch) for a switch but still persists for
    questions. (`tests/agent/test_react_graph.py`.)
  - `ConversationManager.clone_into` duplicates history incl. the trailing deferred
    `ModelResponse` and todos; a resume against the clone is valid.
- **Dispatch behavior:**
  - Triage-removal parity: a plain DM Q&A now dispatches straight to one react run
    and delivers the same reply (no visible change), with no `TriageDecision` run
    and no triage graph.
  - Switch: a run that calls `switch_to_thread` on a Slack/Lark chat → sub-thread
    created, run2 resumes there with full cloned history; a follow-up user message
    in that thread continues the same conversation
    ([channel/base.py:202-208](../../octomate/tentacles/channel/base.py#L202-L208) key routing).
  - `main_only` channel (Napcat/Web): `switch_to_thread` resolves as "stay here,"
    run2 continues in the current conversation — no crash, no orphaned thread.
  - Switch-then-question: run2 asks a question post-switch → human-suspend records
    the **thread** conversation; resume lands there.
- **Manual:** run `main.py` against Slack/Lark; ask to "continue in a thread,"
  confirm the thread receives the continuation and a reply in it keeps context.

## Risks / open questions

- **Cost shift:** every message now hits the full inkling agent (the cheap
  constrained triage screen is gone). This is the intended collapse, but note the
  per-message token bump vs. the old answer-only short path.
- **History duplication:** `clone_into` physically copies messages (same tradeoff
  M2 accepted). A lineage/parent-pointer is a later optimization.
- **Origin conversation after switch:** left intact (fork). Optionally mark it
  relocated so it isn't resurfaced — decide in implementation.
- **Chained switches:** the dispatch re-drive supports it; confirm no key/cache
  aliasing when switching twice (we never have >1 thread depth here — a chat is
  `main`-or-thread, and switch step (a) blocks re-threading from inside a thread).
- **Detecting "switch" in `DeferredToolRequests`:** confirm the pydantic-ai
  surface exposes tool name + `tool_call_id` + args to detect and resolve the call
  (same dependency as M2).

## Out of scope (later / full M2)

- Cross-channel and cross-*platform* switching, candidate sets, `InklingDeps`,
  identity mapping, opening new DMs — all parked in [channel-retargeting.md](channel-retargeting.md).
- The targeted `send_message(target=…)` of M1 (reply-and-targeting) — independent.
