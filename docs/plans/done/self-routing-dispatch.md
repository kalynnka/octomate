# Plan: self-routing dispatch — merge triage+reception into one run; route via the `gate` toolset (`scry` · `summon` · `teleport`)

> **Status:** done · **Owner:** @luhui · **Created:** 2026-07-06 · **Completed:** 2026-07-06
> **Supersedes:** [../cancelled/channel-retargeting.md](../cancelled/channel-retargeting.md)
> (M2 sticky re-home) and [../cancelled/triage-collapse-inchannel-switch.md](../cancelled/triage-collapse-inchannel-switch.md)
> (single-react-loop collapse). Both predated the triage→dispatcher refactor now on
> `main`; this plan is the corrected direction.

## Outcome (landed)

Shipped in `337215c` (`ConversationManager.fork` + `SummonDestination`) and `50b90f0`
(the dispatch collapse); **341 passed, 13 skipped**. Every in-scope goal is delivered;
the `dm` and cross-channel destinations stay parked exactly as written below.

The implementation diverged from this doc's vocabulary — read the body with this map:

- **Package `triage/` → `reflex/`.** Nodes `RunTriage` / `RunReception` / `PrepareReception`
  / `RunAgent` became **`Route` / `React` / `Handoff`**, plus `Teleport` and `ResumeDeferred`.
  Graph: `Awake → Route → React ⟳ {Handoff | Teleport} → End`, plus `ResumeDeferred`.
- **`teleport` is classified by its CallDeferred metadata `kind`, not by tool name** —
  §6's tool-name detection was superseded, so dispatch never matches on a tool name.
- **The `constants` module was removed, not added to** (§4): `TELEPORT_KIND` lives in
  `capabilities/gate.py` beside the tool that emits it, and `SEND_TOOL_NAME` moved to
  `schemas/messages.py`.
- **Durable resume keeps richer run names**, not one collapsed kind (§7): each run is
  named by how the react loop was entered — `react` / `summon` / `teleport` / `resume`.
- **`materialize(destination)` was inlined** into `Handoff` and `Teleport` rather than
  extracted (§3) — two call sites did not earn a shared helper.
- The decision dataclasses `DirectAnswerDecision` / `TriageDecision` / `TriageAction` /
  `TriageDecisionBase` / `TriageDecisionAdapter` are gone; `SummonDecision` is the only one.

Inline source links below point at the pre-implementation `triage/` tree and its line
numbers; follow the map above for the shipped names.

## TL;DR

Today a turn costs **two model runs**: a dedicated *triage screen* (`RunTriage`)
that routes, then a *reception* run (`RunReception`) that does the work
([triage/graph.py](../../octomate/triage/graph.py)). The screen adds a model call
that decides routing **before** the turn actually runs, with a constrained
`output_type`, and duplicates the reception's delivery code.

**Collapse the two runs into one self-routing run.** The channel's entry agent runs
**once** per turn and *is* the router: it answers directly, or uses its **`gate`**
toolset to route — `summon` (hand off to another agent) or `teleport` (continue
itself in a new place) — or defers to a human. A **thin dispatcher** executes
whichever decision the run produced. The `gate` toolset (today's `SummonCapability`,
`scry`/`summon`, [capabilities/summon.py](../../octomate/capabilities/summon.py))
replaces the triage screen.

This is **not** the single-react-loop collapse from the retired plan. We keep a
dispatcher tier because the requirements force one:

## Why keep a dispatcher (the decisive constraint)

The story needs **agent handoff** (inkling→claude, inkling→codex) plus **thread
ownership** and continuing the conversation on a new surface. Two facts make a
dispatch tier load-bearing:

1. **A cross-agent handoff cannot copy history forward.** Claude resumes via its own
   `session_id` (`resume=`, [claude-agent-integration.md](../claude-agent-integration.md)
   Phase 3); Codex via its own SDK thread id
   ([codex-tentacle-pydantic-ai-adaptation.md](../codex-tentacle-pydantic-ai-adaptation.md)).
   You cannot clone inkling's pydantic-ai `ModelMessage` history into a Claude
   conversation and "resume the pending tool call." A cross-agent handoff is
   *inherently* **brief + fresh run** — exactly what `summon`'s `decision.summon`
   brief already does ([summon.py:69-99](../../octomate/capabilities/summon.py#L69-L99)).
2. **Selecting among N agents + tracking who owns a thread is a dispatch concern**
   that cannot live inside one agent's react loop. `Route` already resolves the
   thread owner and skips the screen for owned threads
   ([graph.py:309-334](../../octomate/triage/graph.py#L309-L334)); that fast-path
   stays.

So we delete the redundant *screen call*, **not** the dispatcher. The dispatcher's
job becomes: resolve which agent runs (owner fast-path or channel default) → drive
it once → execute its `summon` / `teleport` / human-deferred decision → loop.

## What is already on `main` (do **not** rebuild)

- **`gate` toolset (today `SummonCapability`)** — `scry`/`summon`, validated against
  `route_keys`, self-summon rejected ([summon.py](../../octomate/capabilities/summon.py)).
  Reception can already summon again mid-run → chained handoff
  ([graph.py:945-960](../../octomate/triage/graph.py#L945-L960)).
- **Thread ownership** — `active_agent_tentacle_id`, `latest_handoff`,
  `ThreadManager.record_handoff` ([thread.py:235](../../octomate/managers/thread.py#L235));
  owned-thread follow-ups skip triage. Conversations are keyed by
  `(thread_id, agent_tentacle_id)`, so a new owner gets its own conversation under the
  same thread.
- **Sub-thread materialization** — `start_sub_thread`, `main_only` fallback
  ([graph.py:649-668](../../octomate/triage/graph.py#L649-L668); [channel/base.py:272](../../octomate/tentacles/channel/base.py#L272)).
- **Durable human-deferred resume** — `ResumeDeferred` + `HumanReviewSuspender` +
  `DeferredActionManager`; `kick` is the durable boundary
  ([base.py:98-109](../../octomate/base.py#L98-L109)).
- **History helpers** — `record_agent_run`, `drop_trailing_deferral`
  ([conversation.py:98,156](../../octomate/managers/conversation.py#L98)).

## The `gate` toolset: three spells for routing a turn

`gate` is the agent's routing spellbook. All three tools decide *where a turn goes and
who handles it* — and each is opaque on its own, so the toolset's instructions **open
with a plain-words statement of what they actually do** (see §4):

| spell | plain meaning | mechanism |
|---|---|---|
| `scry` | list the other agents you can hand off to | reveal the validated route catalog |
| `summon` | hand this conversation to **another agent** (they own it, continue from a brief) | **handoff brief** — history can't cross runtimes |
| `teleport` | move this conversation to a **new place**, keep handling it yourself | **copy the history forward** (`fork`) + resume |

**One operation, two mechanisms.** Continuing a conversation elsewhere is a single idea,
carried out two ways by whether ownership changes:

- **`summon`** — ownership *transmits* to another agent, so a **brief** stands in for the
  history (the next agent, Claude/Codex, resumes via its own session — nothing to copy).
  `summon here` transmits ownership of the *current* thread in place; `summon thread`
  hands off into a fresh sub-thread.
- **`teleport`** — **same** agent, so the full history is **copied forward** into the new
  place and the run resumes seamlessly. Origin left intact (a relocated stub).

**Destinations** (same-platform; **v1 opens only a sub-thread**):
- **`here`** — the current thread; **`summon`-only** (transmit ownership in place).
  `teleport` has no `here`: it always lands on a *new* place, and a same-agent "stay" is
  just not calling it.
- **`thread`** — a new sub-thread off the current chat; needs
  `thread_strategy != "main_only"` → `start_sub_thread`. The **only new surface in v1**.
- *(parked — see Scope)* **`dm`** (a brand-new DM, Slack `conversations.open`) and any
  **different-channel / cross-platform** target.

**Ownership is derived, not stored** — `active_agent_tentacle_id = latest_handoff.to_agent`
over an append-only ledger, with no owner-flag to clear
([thread.py:141-158](../../octomate/schemas/thread.py#L141-L158)). One rule falls out,
enforced in §4:

> Ownership is safe on **bounded / 1:1** surfaces (threads, DMs) and **hazardous on a
> group *main*** (multi-user, multi-topic, unbounded). "Hand back" = record a handoff to
> the channel default — there is no other release.

## Scope

**In scope**
- Collapse `RunTriage` + `RunReception` into one self-routing `RunAgent`; drop the
  separate triage model call and the `DirectAnswerDecision` routing output.
- Keep the owner / flat-thread fast-path; the entry agent is the channel default (or
  the thread owner), not a triage agent.
- Rename `SummonCapability` → **`GateCapability`** (toolset id `gate`); it now holds
  `scry` + `summon` + **`teleport`**, and opens with a plain-words instruction (§4).
- **`summon` gains a `destination`** (`here | thread`), incl. **`here` = transmit
  current-thread ownership**.
- **`teleport`** — same agent continues in a **new sub-thread of the current chat** by
  copying history forward (`fork` + deferred-resume).
- Simplify the durable-resume path to a single run kind (no `triage`/`reception` split).

**Out of scope (parked — the remaining prerequisites)**
- **Opening a brand-new DM** (`dm` destination / `open_dm` / Slack `conversations.open`).
  `conversations.open` is idempotent — the DM may already carry an owner and history, so
  it needs ownership-clobber + copy-splice reconciliation before it's safe. Deferred
  together with the cross-channel work.
- **Cross-*channel* / cross-*platform*** continuation — a destination on a *different*
  `channel_tentacle_id` (Slack `Dxxx` vs Lark `open_id`). Needs the identity registry in
  [../cancelled/channel-retargeting.md](../cancelled/channel-retargeting.md) §0b.
- So the **only surface v1 opens is a sub-thread of the current chat**; `here` (summon)
  reuses the current one.

## Units of work

### 1. Collapse `RunTriage` + `RunReception` → one `RunAgent` node

Rework [triage/graph.py](../../octomate/triage/graph.py):

- **Delete `RunTriage`** and its constrained routing run
  ([graph.py:366-612](../../octomate/triage/graph.py#L366-L612)). Direct answers become
  ordinary agent output (`str` / `list[MessageSegment]`); cross-posting a direct answer
  to another channel is the already-shipped `send_message(target=…)` job
  ([send-toolset.md](send-toolset.md)), so `DirectAnswerDecision` and its
  `target_id` are removed.
- **`RunAgent`** (merged from `RunReception`) runs the resolved agent once with the
  `gate` toolset, streaming/delivering via the existing delivery code
  ([graph.py:773-915](../../octomate/triage/graph.py#L773-L915)). On the result:
  - `summon.decision` set → **`Handoff`** (materialize destination, record handoff,
    fresh brief run of the summoned agent) → loop back to `RunAgent`.
  - `teleport` deferred call → **`Teleport`** (§5) → loop back to `RunAgent` with
    `deferred_tool_results`.
  - human `DeferredToolRequests` → suspender already presented → `End`.
  - else → `End`.
- **`Route`** keeps the owner + flat-thread fast-paths
  ([graph.py:309-361](../../octomate/triage/graph.py#L309-L361)); its "else" branch now
  targets `RunAgent` with the **channel default agent**.

Net nodes: `Awake → Route → RunAgent ⟳ {Handoff | Teleport} → End`, plus `ResumeDeferred`.
`PrepareReception` folds into the shared materialization (§3); `RunTriage` is gone.

### 2. Config: entry agent + summon catalog

Today a channel has `triage` (agent+model) and `receptions`
([graph.py:143-168](../../octomate/triage/graph.py#L143-L168)). After the merge:
- The **entry agent** for a fresh (unowned, non-flat) thread is the channel default —
  repurpose `receptions[0]` or add an explicit `default`. Drop `triage` (or keep it
  only as the default entry model).
- `receptions` stays as the **summonable route catalog** (`available_routes`), unchanged.

### 3. `Destination` + `materialize(destination)` (shared by `summon` & `teleport`)

- **`SummonDestination = Literal["here", "thread"]`** in `octomate/schemas/triage.py`.
  `teleport` takes **no destination arg** in v1 (its only target is `thread`), so a
  teleport-destination type isn't needed yet — unparking `dm`/cross-channel is what
  reintroduces one (which would still exclude `here`).
- **`materialize(destination, source_address, hint) -> ResponseTarget`** — one helper
  (extract from `PrepareReception`, [graph.py:616-673](../../octomate/triage/graph.py#L616-L673)).
  `here` is also the internal **fallback outcome** when a `thread` can't materialize:
  - `here` → the current thread's address unchanged (summon-chosen, or a fallback).
  - `thread` → `start_sub_thread` if `thread_strategy != "main_only"`, else fall back to
    `here` (reuse the existing try/except fallback-to-main).
  Then `ensure` the target thread. `Handoff`/`Teleport` both call this.

### 4. `GateCapability` — rename, add `teleport`, plain-words instruction

Rename `SummonCapability` → `GateCapability` (`toolset id="gate"`); keep `scry`/`summon`,
add `teleport`. Add `TELEPORT_TOOL_NAME = "teleport"` to `octomate/constants.py`.

- **Plain-words instruction (the requested convention).** The toolset's
  `get_instructions()` must open by stating, in common words, *what these tools actually
  do* — the spell names are flavor. Shape:
  > **Gate — decide where this turn goes and who handles it.** If you can answer, just
  > answer; call none of these. Otherwise: **`scry`** lists the other agents you can hand
  > off to; **`summon`** hands this conversation to another agent (they become its owner
  > and continue from a brief you write — `here` in place, or `thread` in a fresh
  > sub-thread); **`teleport`** moves this conversation into a new place (a new sub-thread)
  > that *you* keep handling, carrying everything said so far.

  Adopt the same pattern for the other tool-bearing capabilities (Send, Todo, …) as a
  minor follow-up: one plain-words line up front. This plan establishes it for `gate`.
- **`summon` destination + `here` gate (Case 1).** Add `destination: SummonDestination`
  to `summon` + `SummonDecision`. The dispatcher computes
  `allow_here = not (address.is_group and not address.thread_id)` and passes it to
  `GateCapability`; choosing `here` on a **group main** raises `ModelRetry`
  ([summon.py:87-97](../../octomate/capabilities/summon.py#L87-L97)), steering the model
  to `thread`. Rationale: pinning an owner on a group main routes every gated-in message
  (from *any* user) to one agent indefinitely — a **new** state (today no handoff is ever
  written to a group-main thread, so `active_agent` there is always `None`).
- **`teleport` tool** — a deferred tool on the same toolset (like `ask_questions`):
  ```python
  TELEPORT_TOOL_NAME = "teleport"
  async def teleport(ctx: RunContext[None], hint: str) -> str:
      """Move this conversation into a new sub-thread of the current chat and keep
      handling it; everything said so far comes with you."""
      raise CallDeferred(metadata={"kind": "teleport", "hint": hint})
  ```
  No destination arg (v1 target is always a sub-thread), so `here` is unrepresentable.
  `summon` records a decision read post-run; `teleport` raises `CallDeferred` — both
  coexist in the one `gate` toolset.
- **Release** — an agent ends its own ownership by summoning the channel default (a
  handoff whose `to_agent` is the default cleanly reverts `active_agent`); optional
  human `/release`. This is the concrete "until handed back".

### 5. `teleport` mechanism — copy history forward + resume

- **`Handoff` node (summon)** — `materialize(decision.destination, …)`, then
  `record_handoff` on the **materialized** thread (for `here`, the *current* thread — the
  ownership transfer), and run the summoned agent from `decision.summon` in its own
  `(thread_id, summoned_agent)` conversation. The `active_agent_tentacle_id` fast-path
  routes follow-ups to the new owner
  ([graph.py:309-334](../../octomate/triage/graph.py#L309-L334)). `summon` **hands off —
  it never copies history** (cross-runtime). Replaces today's forced `mode="sub"`
  ([graph.py:606-607](../../octomate/triage/graph.py#L606-L607)).
- **`ConversationManager.fork(source, target)`** — copy history into `target` at the
  database level: a bulk `INSERT` off a `SELECT` of the source rows (built with the
  Arcanus schema class — `select`/`insert(ModelMessage)`, no ORM-model import), keyed to
  fresh order-preserving `uuid7` ids under one new `AgentRun`, then reload + re-cache the
  target. Nothing round-trips through Python message objects; the trailing deferred
  `ModelResponse` (the `teleport` call) is copied like any other row, so the resume is
  valid. Copy todo rows too (keyed by `conversation_id`); memory keys derive from
  `thread_id` and are not carried. **Fail fast if the target conversation is non-empty** —
  copying onto existing messages splices two histories. (v1's target is always a
  freshly-opened sub-thread, so this holds trivially; it's the invariant that keeps a
  future `dm` target honest.)
- **`Teleport` node** — `materialize("thread")` (§3). If it falls back to `here`
  (`main_only`, or already in a thread), resolve the call as "staying here" (no copy).
  Else `fork(current → target)`, build `DeferredToolResults` resolving the
  `teleport` call, re-enter `RunAgent` with `conversation_key=target` + the results →
  run2 resumes via react `ResumeTurn → RunAgent` ([react.py:130-148](../../octomate/capabilities/react.py#L130-L148))
  against the copied history and delivers to the sub-thread.

### 6. Suspender `teleport`/`summon`-awareness

`HumanReviewSuspender.suspend` ([triage/suspender.py:39](../../octomate/triage/suspender.py#L39))
returns `None` (persist nothing) for a `teleport` call — detected by tool name — so run1
ends cleanly and the teleport bubbles to `Teleport`. Questions/approvals unchanged; treat
`teleport` as exclusive of `ask_questions` in one run. (`summon` is not deferred — it
records a decision read post-run — so it needs no suspender change.)

### 7. Simplify durable resume to one run kind

`HumanReviewSuspender` and `ResumeDeferred` branch on `run_name ∈ {triage, reception}`
([graph.py:1011-1070](../../octomate/triage/graph.py#L1011-L1070); [suspender.py:30](../../octomate/triage/suspender.py#L30)).
With one self-routing run there is a single kind — collapse the branching. A plain run
passes `decision=None` (the suspender already accepts `None`); handoff batches keep the
`decision` for continuity.

## Verification

- **Unit:**
  - `teleport` raises `CallDeferred({"kind":"teleport","hint":…})`; suspender returns
    `None` for a teleport but still persists a batch for questions.
  - `ConversationManager.fork` duplicates history **including the trailing
    deferred `ModelResponse`** and todos; a resume against the copy is valid; a non-empty
    target raises.
  - `materialize("thread")` returns `here` on `main_only`/already-threaded, else a
    started sub-thread.
  - `summon` route validation unchanged; `destination` round-trips through `SummonDecision`.
  - `gate` instructions lead with the plain-words statement of each tool.
- **Dispatch behavior:**
  - **Merge parity:** a plain DM Q&A dispatches to **one** run, same reply — no
    `RunTriage`, no `DirectAnswerDecision`, no second model call.
  - **Summon `here` (ownership transfer):** entry agent `summon here` claude → claude
    runs from the brief **in the current thread**, `active_agent_tentacle_id` = claude;
    the next user turn in that thread skips the router and lands on claude.
  - **Summon `thread`:** handoff spins a sub-thread, summoned agent owns it, follow-ups
    route there; the origin chat is untouched.
  - **Group-main `here` rejected (Case 1):** `summon here` on a group main → `ModelRetry`;
    the model re-picks `thread`; no handoff is written to the group-main thread.
  - **`teleport` (same agent):** sub-thread created, run2 resumes there with the copied
    history; a follow-up continues it.
  - **`main_only` / already-threaded:** `teleport` and `summon thread` fall back to `here`
    — no crash, no orphan surface.
  - **Teleport-then-question:** run2 asks a question post-teleport → human-suspend records
    the **sub-thread** conversation; resume lands there.
  - **Chained summon + teleport** in one turn: no key/cache aliasing.
- **Manual:** run `main.py` against Slack/Lark; confirm (a) a normal question is one
  run, (b) "let claude take this here" transfers ownership of the current DM/thread and a
  follow-up stays with claude, (c) "continue in a thread" teleports into a sub-thread and
  keeps context.

## Risks / open questions

- **Group-main ownership (Case 1) — resolved:** `here` is disallowed on a group main
  (§4 `allow_here` → `ModelRetry`); group handoffs use `thread`, so an owner is only ever
  pinned on a bounded surface. **Deferred refinement:** idle-TTL auto-release in `Route`
  keyed on `latest_handoff.created_at` (a drop-in later).
- **Sticky DM-main ownership — product call:** `summon here` in a DM main hands the whole
  DM to the summoned agent until released. That's the intended "claude is my assistant
  here" behavior (1:1, no cross-user hazard), but confirm you want it sticky vs. bounded.
- **Cost shift:** every message now runs the full entry agent (the cheap screen is
  gone) — note the per-message token bump; point the channel default at a cheaper model
  where appropriate.
- **`summon` doesn't hard-stop the run** — decision read post-run
  ([graph.py:945](../../octomate/triage/graph.py#L945)); a model that keeps talking after
  `summon` emits stray text before handoff. Acceptable today.
- **History duplication** — `fork` physically copies messages; a lineage pointer
  is a later optimization.
- **Origin conversation after a `teleport`** — left intact (teleport copies history
  forward; the origin is a relocated stub); optionally mark it relocated.
- **Detecting `teleport`/`summon` in `DeferredToolRequests`** — confirm the pydantic-ai
  surface exposes tool name + `tool_call_id` + args.
- **Parked destinations** — a brand-new `dm` (idempotent `conversations.open`, needs
  owner/copy reconciliation) and cross-*channel*/platform targets (need the identity
  registry, [../cancelled/channel-retargeting.md](../cancelled/channel-retargeting.md) §0b).
  The `fork` non-empty guard (§5) and the derived-ownership model are what will
  keep them safe when unparked.
