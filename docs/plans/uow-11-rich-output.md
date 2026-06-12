# Plan: UoW-11 — inkling rich output (`list[MessageSegment]`) via the normalizer

> **Status:** 11a + 11b + 11c implemented (uncommitted). Slack/Lark render image
> (upload + native message) and card (blocks / interactive) segments via the
> `TimelineState.answer_segment` hook; Default/NapCat keep the `str(segment)` text
> fallback · **Parent:** [agent-event-stream.md](agent-event-stream.md) UoW-11
> **Depends on:** UoW-3 (normalizer built), UoW-6 (`present_output` renderers), UoW-9
> (reception → `consume()`), UoW-13 (dead push paths gone).

## TL;DR

Today inkling replies are **`str`**, streamed as **raw** `TextPart` events through the
react graph and rendered by `consume()` as plain answer text. UoW-11 lets inkling reply
with a **`list[MessageSegment]`** (text + markdown + image + card) that renders natively
per platform. The enabler is the **dormant normalizer** (`Agent.stream_events`), which
maps a structured output into one `ResultSegmentEvent` per segment — but it is dormant on
*three* levels, all of which this UoW must wake up.

## The gap — why it's not a one-line flip

| Layer | Today | Needed |
|---|---|---|
| inkling's agent | **vanilla `pydantic_ai.Agent`** ([inkling/base.py:62](../../octomate/tentacles/agent/inkling/base.py#L62)) — has no `stream_events` | the octomate `capabilities.agent.Agent` subclass |
| react streaming | `agent.run_stream_events(...)` → **raw** events ([react.py:188](../../octomate/capabilities/react.py#L188)) | `agent.stream_events(...)` → normalized `StreamEvents` |
| inkling output | `output_type=[str, DeferredToolRequests]` ([inkling/base.py:66](../../octomate/tentacles/agent/inkling/base.py#L66)) | `[list[<content segment>], DeferredToolRequests]` |

For a **`str`** reply the raw route already works (model streams `TextPart` → `consume`
renders it). For a **`list[MessageSegment]`** reply the model fills an *output tool* — the
raw stream carries only tool arg-deltas, which `consume()` does **not** render as an
answer. Only the normalizer turns those validated args into `ResultSegmentEvent`s. So
rich output *requires* routing the react stream through `stream_events`.

A second snag: `Agent.stream_events` ([capabilities/agent.py:92](../../octomate/capabilities/agent.py#L92))
currently accepts only `user_prompt, output_type, message_history, deferred_tool_results,
conversation_id, deps, model`. The react `RunAgent` passes **~18** params to
`run_stream_events` (`instructions, model_settings, usage_limits, usage, metadata,
output_retries, infer_name, toolsets, builtin_tools, capabilities, spec`, …). Swapping the
call as-is would silently drop the toolset, instructions, and capabilities. So
`stream_events` must reach param parity with `run_stream_events` first.

## Design — three sub-steps, each independently landable + verifiable

### 11a — wake the normalizer in the react graph (no behaviour change)
Keep inkling `str`-output; just route streaming through the normalizer so the path is
proven and the event types are unified.

1. **Param parity** — extend `Agent.stream_events` ([capabilities/agent.py](../../octomate/capabilities/agent.py))
   to accept + forward to `self.iter(...)` the full set `run_stream_events` takes
   (`instructions, model_settings, usage_limits, usage, metadata, output_retries,
   infer_name, toolsets, builtin_tools, capabilities, spec`). Verify each is a real
   `iter()` kwarg in pydantic-ai 1.93 (drop any that isn't and note it).
2. **inkling builds the octomate `Agent`** — `build_inkling_agent`
   ([inkling/base.py:55](../../octomate/tentacles/agent/inkling/base.py#L55)) returns
   `octomate.capabilities.agent.Agent(...)` instead of `pydantic_ai.Agent(...)`. It's a
   drop-in subclass; `run`/`run_stream_events` keep working for the non-stream + RunTriage
   paths.
3. **react uses `stream_events` when streaming** — `RunAgent.run`
   ([react.py:187-212](../../octomate/capabilities/react.py#L187-L212)) calls
   `agent.stream_events(...)` instead of `agent.run_stream_events(...)`, forwarding each
   event to `event_send_stream`. Type `ReactDeps.agent` as the octomate `Agent` (it has
   both methods).
4. **Widen `ReactStreamEvent`** ([react.py:46](../../octomate/capabilities/react.py#L46))
   from `AgentStreamEvent | AgentRunResultEvent | ActionBatchEvent` to include the
   normalizer's `ResultTextDeltaEvent | ResultSegmentEvent` (i.e. fold in `StreamEvents`).
   `consume()` already matches all of these.

   **Acceptance:** existing reception tests still green; a streamed `str` reply now arrives
   as `ResultTextDeltaEvent` (was raw `TextPart`) and renders identically. Capabilities
   (todo events), deferred round-trip, and history are unchanged.

### 11b — flip inkling output to `list[<content segment>]`
1. Define the **output-segment subset** (see Decision 1) — e.g.
   `OutputSegment = TextSegment | MarkdownSegment | ImageSegment | CardSegment` — and set
   inkling `output_type=[list[OutputSegment], DeferredToolRequests]` (the default agent
   at :66 + the react default at :326). `InklingOutput` alias updates accordingly.
2. **System prompt** — teach the model to emit segments (one markdown segment for prose;
   image/card when relevant). Keep it cheap: prose-only replies stay a single
   `MarkdownSegment`.
3. RunTriage / non-stream `markdown_from_output` already handles non-str output via
   `format_stream_value`; confirm it renders a `list[segment]` sanely (fallback path).

   **Acceptance:** a reception reply of `[MarkdownSegment("…")]` streams as one
   `ResultSegmentEvent` and renders as the answer on Slack/Lark; deferred + todo paths
   unaffected.

### 11c — render segments richly (replace the `str(segment)` placeholder)
Today `drive_timeline`'s `ResultSegmentEvent` case calls `answer_delta(str(segment))` — a
placeholder. Give the timeline state a real segment renderer:
- `TextSegment`/`MarkdownSegment` → existing answer text path.
- `ImageSegment` → upload via `ink` + send (per platform).
- `CardSegment` → platform card.

This is per-platform feeler work (Slack first, then Lark; Default = `str(segment)` text
fallback + the "no streaming transport" message). **Scope decision 2** below — this can be
its own follow-up if 11a+11b land first with text/markdown only.

## Decisions to settle before coding
1. **Output-segment subset.** Recommend `Text | Markdown | Image | Card` (exclude
   `At`/`Reply`/`File` initially — they're inbound-leaning; add later if needed). The
   model rarely needs `At`/`Reply` in a reply.
2. **How far in this UoW.** Recommend land **11a + 11b + text/markdown rendering** as
   UoW-11, and split **image/card rendering (11c)** into a follow-up (it's per-platform
   upload plumbing, lower risk to defer). Confirms the rich-output *contract* end-to-end
   while keeping the diff bounded.
3. **`str` vs `list` default.** The plan flips inkling to `list`. Alternative: keep `str`
   default + opt-in `list` per run. Recommend the flip (one contract) since `[Markdown(…)]`
   subsumes `str`.

## Risks
- 🔴 **`stream_events` ≠ `run_stream_events` semantics.** The normalizer drives `iter()`
  directly and re-applies capability `wrap_run_event_stream` per node; must confirm the
  deferred-tool **resume** (`deferred_tool_results`) and **suspend** (the react
  `ResolveDeferred` → `ActionBatchEvent`) still behave when streaming via `stream_events`.
  11a's test must cover a deferred reception round-trip.
- 🟡 **Partial-validation churn.** `stream_events` validates `list[MessageSegment]`
  per-delta with `allow_partial=True` and emits new segments as they complete; a half-built
  segment shouldn't emit twice. The normalizer already guards with `emitted_segments`
  ([agent.py:155-164](../../octomate/capabilities/agent.py#L155-L164)) — cover with a
  multi-segment `TestModel` stream.
- 🟡 **Model cost/quality.** Forcing structured-segment output can degrade small models;
  the system prompt must make "one markdown segment" the easy default.

## Verification
- `tests/test_capabilities_agent.py` (normalizer): param-parity + multi-segment emission
  via `TestModel`.
- `tests/test_triage_graph.py` / reception: streamed reply now `ResultSegmentEvent`;
  deferred round-trip intact.
- `tests/test_channel_chromos.py`: Slack/Lark `consume()` renders a `[Markdown]` reply.
- `.venv/bin/python -m pytest -q` green; `ruff` clean; CLI `pyright` no new errors.
- Land per sub-step (11a → 11b → 11c) as separate commits on `feat/event-stream-renderers`.
