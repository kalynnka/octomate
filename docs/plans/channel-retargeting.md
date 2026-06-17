# Plan: M2 — sticky channel re-targeting (`switch_target`) + triage collapse

> **Status:** designed · **Owner:** @luhui · **Created:** 2026-06-16
> **Parent:** [reply-and-targeting.md](reply-and-targeting.md) (M1 = targeted
> `send_message`; this is **M2**, the sticky re-home).

## Context

Today, *where* a turn's response goes is decided **before** the agent runs, by a
separate triage model inside a dedicated graph (`Awake → Route → RunTriage →
PrepareReception → RunReception`, [graph/triage.py](../../octomate/tentacles/agent/inkling/graph/triage.py)).
We want the **agent itself** to decide, at runtime, to **continue the discussion
in another channel/thread** ("let's move this to a thread", "continue on Lark") —
a sticky re-home. This folds triage's routing job into the agent runtime and lets
the triage *screen* (the cheap pre-model) be dropped entirely.

This is **M2** of the broader outbound-targeting effort. **M1** (a `target` arg on
the existing `send_message` for one-time notices/cross-posts — see
[reply-and-targeting.md](reply-and-targeting.md)) ships first and introduces the
shared `InklingDeps` foundation; M2 builds on it.

### Decisions settled (design discussion)

- **Mechanism:** `switch_target` is a **deferred tool** (raises `CallDeferred`,
  like `ask_questions`). It ends run1 cleanly; the **dispatch graph resolves it**
  (no human wait) and resumes run2 — "two runs, seamless from the outside."
- **Conversation on switch:** **fork + copy.** Materialize the new target's
  conversation, **copy run1's full message history (and per-conversation state)
  into it**, then resolve the deferred call and **resume run2 in the new
  conversation**. The copy is what makes the deferred-resume valid in the fork
  (the pending tool call travels with the history) *and* gives future turns in the
  new thread the full context.
- **Triage collapses:** no cheap-answer screen; routing becomes `switch_target`.
  The graph reduces to a slim dispatch graph.
- **Hard constraint that forced this shape:** `DeferredResolver.resolve(requests)`
  gets only the requests — no `ctx`/state/deps
  ([capabilities/deferred.py:23-26](../../octomate/capabilities/deferred.py#L23-L26))
  — and the react loop's run2 always uses the fixed `ReactState.conversation_key`
  ([capabilities/react.py:162-165](../../octomate/capabilities/react.py#L162-L165)).
  So a react-level resolver can neither fork nor relocate; the switch must be
  handled one level up, in the dispatch graph, which can start run2 under a new key.

## Approach

A new `SwitchTargetCapability` exposes the deferred `switch_target` tool. The
collapsed dispatch graph detects a switch in the run result, **materializes the
target, clones the origin conversation into it, and re-enters its run node in the
new conversation resuming the deferred call.** Questions/approvals keep today's
human-suspend path; only the suspender gains switch-awareness so it doesn't
persist a phantom human batch for a switch.

### Mechanism walkthrough (case: "continue this in a thread")

1. run1 executes in the origin conversation. The agent calls `switch_target(target,
   hint)`, which `raise CallDeferred(metadata={"kind": "switch", ...})`. run1 ends
   with `output = DeferredToolRequests`; react records run1's messages to the
   **origin** conversation ([react.py:239-245](../../octomate/capabilities/react.py#L239-L245)).
2. The dispatch run node receives the result, inspects the deferred calls, finds
   `switch_target` → routes to a `Switch` node (not the human-suspend path).
3. `Switch` node:
   a. Resolve `target_id → ResponseTarget`; materialize via
      `channel.start_sub_thread(...)` when a sub-thread is needed (reusing
      `PrepareReception`'s logic, [triage.py:358-388](../../octomate/tentacles/agent/inkling/graph/triage.py#L358-L388)).
   b. `ensure()` the new target conversation, then **clone the origin
      conversation's full history (and todos) into it** (new
      `ConversationManager.clone_into(...)`).
   c. Build `DeferredToolResults` resolving the `switch_target` call (e.g.
      `"continuing in <target>"`).
   d. Re-enter the dispatch run node with `conversation_key = new_key`,
      `deferred_tool_results = <results>`, and `InklingDeps.current_target = new
      target`.
4. run2 resumes via `ResumeTurn → RunAgent(deferred_results=…)`
   ([react.py:119-137](../../octomate/capabilities/react.py#L119-L137)):
   `agent.run` gets `message_history = new_conversation.messages` (the cloned
   history, which contains the pending switch tool call) + the deferred results, so
   the resume is valid. run2's output streams/delivers to the **new** target and
   records to the **new** conversation.
5. Future user turns in the thread map to the new conversation via existing key
   routing (ingest builds the key from `thread_id`,
   [channel/base.py:202-208](../../octomate/tentacles/channel/base.py#L202-L208))
   — they see the full cloned history. The origin conversation is left intact
   (optionally marked relocated — see open questions).

## Units of work

### 0. Prerequisite — `InklingDeps` (shared with M1)

If M1 hasn't landed it: introduce `InklingDeps(channels, conversation_manager,
current_target)` and switch the inkling agent off `None` deps
([main.py:74](../../main.py#L74) `deps_type`,
[tools.py:8](../../octomate/tentacles/agent/inkling/tools.py#L8)
`FunctionToolset[None]`, [base.py:55](../../octomate/tentacles/agent/inkling/base.py#L55)
type param + the `deps` params on `run`/`iter_graph_events`). Seeded per-run by the
dispatch node from `ctx.deps.channels` + the resolved target.

### 0b. Prerequisite — target materialization & candidate filtering

**Opening the destination is the hard, platform-gated part.** A switch can only go
to a target the system can actually create/address. Current capabilities
(confirmed):

- **Sub-thread of the *current* chat** — works only on channels with
  `thread_strategy != "main_only"`: **Slack** + **Lark** (`flat_thread`), which
  start a thread by posting the hint message and using its id as `thread_id`
  ([slack/base.py:347-358](../../octomate/tentacles/channel/slack/base.py#L347-L358),
  [lark/base.py:147-158](../../octomate/tentacles/channel/lark/base.py#L147-L158)).
  **Napcat** + **Web/Vercel** are `main_only` — no sub-threads at all
  ([channel/base.py:230-240](../../octomate/tentacles/channel/base.py#L230-L240)
  default just warns + presents in the same key).
- **A different conversation the system already knows** — addressable because its
  `ConversationKey` already exists.

**Not possible in v1 (must be excluded from the candidate set):**

- **Opening a brand-new DM/chat** with a user who has no existing conversation —
  there is **no** `open_dm`/`conversations.open` primitive on any ink; Slack in
  particular needs the DM channel id created first. (Lark/Napcat *can* send to a
  raw `user_id`, but Slack cannot — so it isn't uniformly available.)
- **Cross-*platform* switching** (e.g. Slack→Lark) — keys are copied as-is
  ([triage.py:358-366](../../octomate/tentacles/agent/inkling/graph/triage.py#L358-L366)),
  but identity formats differ (Slack DM id `Dxxx` vs. Lark `open_id`) and **no
  cross-platform identity mapping exists**. This is the 🔴 prerequisite already
  deferred in [reply-and-targeting.md](reply-and-targeting.md).

**So the v1 candidate set is effectively:** *(a)* "a new sub-thread of the current
chat" — offered only when `current channel.thread_strategy != "main_only"`; plus
*(b)* same-channel conversations the system already knows for this user. The
candidate builder (UoW-1) **must filter by materializability** — never offer a
target the channel can't open. When materialization fails at switch time, fall
back to staying in the current target (reuse `PrepareReception`'s try/except
fallback-to-main, [triage.py:371-388](../../octomate/tentacles/agent/inkling/graph/triage.py#L371-L388))
and tell the agent via the resolved tool result.

> Opening new chats/cross-platform targets (Slack `conversations.open`, an identity
> registry) is a **separate prerequisite** for a later phase — out of scope here.

### 1. `SwitchTargetCapability` + `switch_target` tool

New `octomate/capabilities/switch.py`, following the `SendCapability`/
`TodoCapability` pattern ([capabilities/send.py:71-85](../../octomate/capabilities/send.py#L71-L85),
[capabilities/todos.py:343-355](../../octomate/capabilities/todos.py#L343-L355)):

- `build_switch_toolset()` → `FunctionToolset[InklingDeps]` with:
  ```python
  @toolset.tool(name=SWITCH_TOOL_NAME)
  async def switch_target(ctx: RunContext[InklingDeps], target: str, hint: str) -> str:
      # validate target against ctx.deps candidate set; fail fast on unknown id
      raise CallDeferred(metadata={"kind": "switch", "target": target, "hint": hint})
  ```
- The candidate set offered to the model is the **materializable** set from §0b
  (filtered per `thread_strategy` + already-known conversations), not "every
  channel". The tool validates `target` against that set and fails fast on an
  unknown/unmaterializable id.
- `get_instructions()`: when + how to switch, the available-targets block (reuse
  `ResponseTarget.__str__`, [triage.py:56-61](../../octomate/tentacles/agent/inkling/graph/triage.py#L56-L61)),
  and the honest caveat that cross-*channel* sticky only carries future turns if
  the user actually posts there (threads carry naturally).
- Register in [main.py:78-82](../../main.py#L78-L82) alongside `SendCapability`.
  Add `SWITCH_TOOL_NAME` to [octomate/constants.py](../../octomate/constants.py).

### 2. `ConversationManager.clone_into(...)`

New method: copy a source conversation's message history into a target
conversation (created via `ensure`), as one `AgentRun` of cloned messages — mirror
`record_agent_run`'s `vars(m)` re-blessing ([managers/conversations.py:105-131](../../octomate/managers/conversations.py#L105-L131)),
then `refresh` the target so its cached `messages` include the clone. Must
preserve the trailing deferred `ModelResponse` (the switch tool call) so the
resume is valid — i.e. do **not** drop the trailing deferral here.

### 3. Collapse triage → dispatch graph with a `Switch` node

Rework [graph/triage.py](../../octomate/tentacles/agent/inkling/graph/triage.py):

- **Keep:** `Awake` (minus the triage-screen short-circuits), `ResumeDeferred`
  (human-resume path).
- **Replace** `Route` + `RunTriage` + `PrepareReception` + `RunReception` with a
  single **`RunAgent`** dispatch node that:
  - builds `InklingDeps` (current target) and runs the inkling agent in the
    current conversation, streaming/delivering the return value (reuse
    `RunReception`'s stream vs. non-stream delivery, [triage.py:443-498](../../octomate/tentacles/agent/inkling/graph/triage.py#L443-L498));
  - on result: `DeferredToolRequests` containing a `switch_target` call → `Switch`;
    other `DeferredToolRequests` (questions/approvals) → suspend/End as today;
    else → `End(TriageResult)`.
- **Add `Switch` node** implementing the walkthrough §3a–d above, then loops back to
  `RunAgent` with the new key + deferred results. Reuse `PrepareReception`'s
  target-key derivation + `start_sub_thread` fallback-to-main.
- **Delete:** `TriageDecision` model run, `TRIAGE_INSTRUCTIONS`, candidate/
  `target_id` routing, the `Route` flat-thread screen.
- `Octomate.kick` unchanged in shape ([base.py:78-88](../../octomate/base.py#L78-L88)).

> **Occam check:** confirm the dispatch loop still earns `pydantic_graph` vs. a
> plain async loop around `InklingTentacle.run`. Durable re-entry for *human*
> deferred resume (a second `kick` with `DeferredActionBatchResponse`) is the
> reason to keep a thin graph — default to keeping it.

### 4. Suspender switch-awareness

`HumanReviewSuspender.suspend` ([graph/suspender.py:38-85](../../octomate/tentacles/agent/inkling/graph/suspender.py#L38-L85))
must **not** persist/present a batch when the requests are a `switch_target` call
(detect by tool name): return `None` so run1 ends cleanly and the switch bubbles
to the dispatch `RunAgent` node for resolution. Questions/approvals unchanged.

### 5. Carry per-conversation state (todos)

`TodoManager` is keyed by `conversation_id` ([capabilities/todos.py:182-199](../../octomate/capabilities/todos.py#L182-L199)).
As part of the clone (§2), copy todo rows from origin → target conversation id so
the agent's in-flight task list survives the switch. (Memory keys derive from
`thread_id` and are intentionally **not** carried — note in code.)

## Verification

- **Unit:**
  - `ConversationManager.clone_into` duplicates history (incl. the trailing
    deferred `ModelResponse`) and todos into the target; resume against the clone
    is valid.
  - `switch_target` raises `CallDeferred` with the switch metadata; suspender
    returns `None` (no batch) for a switch request but still persists for
    questions. Extend `tests/agent/test_react_graph.py`,
    `tests/channels/test_reply_targeting.py`.
- **Behavior (graph-level):**
  - Case 5: a run that calls `switch_target` into a new sub-thread → run2 resumes
    in the forked thread conversation with full history; the next user turn in that
    thread continues it. (`tests/agent/test_inkling.py` style.)
  - Initial routing (old triage parity): a DM whose first action is a switch →
    re-homes with no visible interruption.
  - Switch-then-question: run2 asks a question post-switch → human-suspend records
    the **new** conversation; resume lands there.
  - Regression: same-channel Q&A unchanged after the triage screen is dropped.
- **Manual:** run the app (`main.py`) against a Slack/Lark channel; ask the agent
  to "continue in a thread", confirm the thread receives the continuation and a
  follow-up message in the thread keeps context.

## Risks / open questions

- **Target materialization is platform-gated (see §0b)** — only Slack/Lark
  sub-threads + already-known conversations are viable in v1; new DMs and
  cross-platform targets need separate prerequisites (Slack `conversations.open`,
  an identity registry). The candidate filter and the switch-time
  fallback-to-current are what keep this safe.
- **History duplication cost** — clone physically copies all messages. Acceptable
  per the "copy all ctx" decision; a lineage/parent-pointer link is a future
  optimization if duplication becomes heavy.
- **Origin conversation after switch** — left intact (fork). Optionally set its
  `status` to a relocated marker so it's not resurfaced; decide in implementation.
- **Switch + other deferrals in one run** — assume `switch_target` is exclusive
  (a run that switches doesn't also `ask_questions`); validate/guard in the tool.
- **Chained switches** — supported by the loop; confirm no key/cache aliasing
  issues when switching twice.
- **Streaming across the boundary** — run1 and run2 are separate react runs with
  separate timelines; confirm the second timeline opens cleanly on the new target
  and the first closes (reuse `RunReception` streaming, [triage.py:452-475](../../octomate/tentacles/agent/inkling/graph/triage.py#L452-L475)).
- **Dispatch detection of "switch" in `DeferredToolRequests`** — confirm the
  pydantic-ai `DeferredToolRequests`/`DeferredActionManager` surface exposes tool
  name + `tool_call_id` + args needed to detect and resolve the call.
