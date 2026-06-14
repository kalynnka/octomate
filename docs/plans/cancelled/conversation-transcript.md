# Plan: the conversation transcript (user-facing message store)

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-12
> **Feeds:** [send-toolset.md](send-toolset.md) (the record hop), the web
> conversation view ([web-channel.md](web-channel.md)), and the future
> **history-search toolset**.

## TL;DR

Octomate persists the **model's working memory** (`ModelRequest`/`ModelResponse`
rows on the conversation) but not the **conversation as humans saw it**. Three
needs triangulate that gap:

1. **History search (future toolset)** — the agent should search what users and
   the assistant *said*, never raw model messages (tool calls, thinking, retry
   parts make that a definitional headache).
2. **The conversation-layer view + compaction survival** — a messages-only
   listing (web view, exports) that stays intact when model-history compaction
   eventually rewrites the working memory. *(Full-fidelity web replay is NOT
   this store's job — see the replay section.)*
3. **Mid-run sends** — the send tool needs a durable record independent of the
   model-message encoding.

Answer: a **transcript** store — an append-only copy of user/assistant-facing
messages in segment form, written by the system at the moments content actually
crosses the human boundary.

## The store

```python
class TranscriptMessage(BaseTransmuter):          # arcanus table: transcript
    id: uuid (identity)
    conversation_id: uuid                          # FK conversations
    role: Literal["user", "assistant"]
    segments: list[MessageSegment]                 # JSON column; the same vocabulary
    run_id: str | None                             # assistant entries: which run said it
    platform_message_id: str | None                # when known (IM sends/replies)
    created_at: datetime
```

- Segment-typed, not text: search can still flatten (`str(segment)` / a derived
  text column) while replay renders rich content faithfully.
- Plus a `TranscriptManager` (arcanus session pattern, like `TodoManager`):
  `record_user(...)`, `record_assistant(...)`, `list(conversation_id, …)`, and a
  search entry (`search(conversation_id?, query, role?)` — naive `LIKE`/contains
  first; ranking later if needed).

## The writers — where content crosses the human boundary

| Moment | Writer | Role |
|---|---|---|
| Inbound platform message dispatched | `ChannelTentacle.ingest` → dispatch (where the `MessageEvent` already exists in segment form) | `user` |
| Mid-run send | the send tool's **record** hop ([send-toolset.md](send-toolset.md) §1a) | `assistant` |
| Final reply | where the run result is rendered (reception's consume/markdown paths; triage `answer`) | `assistant` |
| Web UI turns | *transitional:* the dev_ui adapter (user prompt in, reply out). Once [web-channel.md](web-channel.md) lands, this row **disappears** — web turns flow through the same `ingest` / send / reply writers as every channel | both |

Writers are **system-side and programmatic** — the agent never writes the
transcript directly; it happens as a side effect of real sends/receives, so the
transcript cannot disagree with what users actually saw. The web-channel plan is
what collapses the writer set to one per moment: no surface-specific writers.

## The readers

- **Web conversation view / replay's conversation layer**: list by conversation →
  render segments. Full-fidelity replay itself is the **synthesized event
  stream** (§ above); the transcript backs the messages-only view and the spans
  compaction will eventually erase from model messages.
- **History-search toolset** (future, todo-pattern capability):
  `search_history(ctx, query)` scoped via `ctx.conversation_id` (or wider once
  authz says so) over user/assistant entries only — the model never greps model
  messages.
- Anything else that wants "what was said": digests, analytics, exports.

## Web replay — the same stream, re-synthesized; and why activity stays out

Replay should present **the same picture as the live stream** (thinking, tool
calls, todos, mid-run sends) through **the same renderer** (the
[web-channel.md](web-channel.md) timeline). The live stream is ephemeral, so
replay **re-synthesizes** it from the durable stores — no third store needed:

- **Stored model messages** carry almost the entire stream: thinking/answer
  parts replay as completed part events (no token deltas — a finished block
  renders at once), tool calls/returns replay as tool events, and — decisively —
  **capability events ride along in `ToolReturnPart.metadata`** (where the todo
  and send capabilities stash them), so `Todo*Event` / `MessageSentEvent` re-emit
  **in-position**, rehydrated via their `event_kind` discriminators.
  *(Verify at implementation: the metadata round-trip through the parts JSON
  column; if it doesn't survive, a coarse append-only event log — no deltas —
  is the fallback.)*
- **Deferred batches** replay their state from the existing batches table.
- The synthesized stream feeds the same `WebTimelineState` → identical chunks as
  live. One rendering path, live and replay.

The **transcript is therefore not the replay backbone**. Its roles are: **search**
(the founding motivation), the cheap **conversation-layer view** (messages-only
listing, exports), and **compaction survival** — when model-history compaction
eventually lands, replay-from-messages degrades for compacted spans while the
conversation record stays intact.

Tool calls/thinking remain **deliberately excluded** from the transcript:
including them would collapse the store's identity into a second, lossier copy
of model messages that must be kept consistent forever. The distinction, kept
crisp:

| | model messages | transcript |
|---|---|---|
| what it is | the agent's **working memory** | the **human-boundary record** |
| contains | thinking, tool calls/results, retries, system parts | user/assistant messages only, as typed segments |
| sends/replies encoded as | tool-call args | first-class segments |
| mutability | trimmed today (`drop_trailing_deferral`), compaction tomorrow | append-only, immutable |
| direction | fed back to the model every run | never fed back |

The mutability row is decisive: when model history is eventually compacted, an
activity-bearing transcript would become the only surviving trace copy — quietly
a second model memory, the explicit non-goal. Conversation content surviving
compaction is the feature; activity surviving it would be scope creep.

## Non-goals / boundaries

- **No activity entries.** Thinking/tool calls never enter the transcript (see
  above) — run-trace replay derives from model messages.
- **Not a second model memory.** The react graph keeps loading model messages via
  `ConversationManager`; the transcript is never fed back as history. One-way.
- **Not retroactive.** Existing conversations start empty; no backfill pass.
- **No dedup magic.** Each writer records exactly what it sent/received; if the
  prompt-level double-delivery guard fails (send + reply repeating content), the
  transcript honestly contains the duplicate the user honestly received.

## Open questions
1. Search scope for the toolset: same-conversation only first; cross-conversation
   needs the targeting/authz story.
2. Whether the timeline's `message_id` return (consume) should be recorded onto
   the assistant entry after the fact (nice for future edit/reply handles). Note
   web messages have no platform id — the **transcript entry id** is the natural
   platform-agnostic handle ([reply-and-targeting.md](reply-and-targeting.md)
   leans on this).
3. Ordering with [web-channel.md](web-channel.md): reconnect replay synthesizes
   from model messages, so the web channel does **not** hard-depend on this
   store — only the messages-only view and compaction survival do. Order freely.

## Verification (when implemented)
- Manager CRUD + search round-trip (in-memory engine, the todo-manager pattern).
- Writer integration: ingest records `user`; send tool records `assistant`;
  reception reply records `assistant` with `run_id`.
- Gates: pytest / ruff / CLI pyright.
