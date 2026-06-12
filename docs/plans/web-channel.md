# Plan: the web channel (wrap the dev_ui adapter into a ChannelTentacle)

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-12
> **Builds on:** [agent-event-stream.md](agent-event-stream.md) (consume/timeline
> hooks, UoW-12 event stream) · **Simplifies:** [send-toolset.md](send-toolset.md),
> deferred round-trip on web · **Pairs with:** [conversation-transcript.md](conversation-transcript.md)
> (reconnect/replay).

## TL;DR

Promote the web UI from a sidecar (`GraphAdapter` driving the react graph
directly) to a **real `ChannelTentacle`**. The web channel consumes the agent's
event stream exactly like Slack/Lark — but its renderer and feelers, instead of
calling a platform API, **emit protocol events onto an in-memory output stream**
that the HTTP layer serves to the browser (SSE). The web becomes "just another
channel": triage, the timeline pipeline, deferred actions, sends, todos — all
land on web with zero parallel machinery, now and for every future feature.

The enabling fit: the `TimelineState` hook surface (`thinking_start/delta/end`,
`answer_delta`/`answer_segment`, `tool_start/end`, `todo`) maps ~1:1 onto the UI
chunk vocabulary (reasoning/text/tool/data chunks). A web timeline state is just
a **chunk emitter**.

## Shape

```
browser ── POST message ──▶ web routes ── ingest/kick ──▶ triage ─▶ reception
   ▲                            │                                      │
   │            create + register per-run output stream               │
   └── SSE ◀── output stream ◀── WebChannel.consume(key, events) ◀────┘
                                  │ timeline hooks → chunks
                                  │ feelers (questions/approvals/markdown)
                                  │   → more chunks on the same stream
```

- **`WebTentacle(ChannelTentacle[...])`** — "ink" = a per-run output stream
  writer (in-memory object streams keyed by conversation/run); "chromo" =
  trivial (the inbound POST body is already structured; outbound conversion is
  the renderer's job). Platform-shaped ABC members (`inspect`,
  `get_user_profile`, media download) get trivial dev implementations.
- **`WebTimelineState(TimelineState)`** — every hook emits the corresponding
  chunk(s) onto the output stream; `answer_segment` emits message content
  (replacing the OctomateUIEventStream reply-part logic); `todo` emits the
  data-todo chunk.
- **Feelers** — `markdown`/`ask_questions`/`approvals` emit chunks too; the
  deferred round-trip becomes the standard one: `ActionBatchEvent` → question/
  approval chunks → UI form → answer POST → `DeferredActionBatchResponse` →
  resume. The Vercel deferred-call special path retires.
- **HTTP layer** — `handle_request`: resolve key → create + register the output
  stream → dispatch the message (kick) as a task → stream chunks until run end →
  close. Reconnect/refresh **replays the same way it streams**: a synthesizer
  rebuilds the event stream from stored model messages (completed parts; tool
  events; capability events rehydrated from `ToolReturnPart.metadata` —
  in-position todos and sends) and feeds it through the same `WebTimelineState`,
  then re-attaches to a live stream if one exists. The transcript backs the
  messages-only view and compacted spans
  ([conversation-transcript.md](conversation-transcript.md)).
- **`GraphAdapter` retires**; `OctomateUIEventStream` either retires with it or
  shrinks to the protocol-encoding edge (Decision 1).

## What this dissolves elsewhere

- **send-toolset**: the "channel-less conversation" case disappears — web
  conversations have a channel, so the tool's *deliver* hop is uniform
  (`send_segments` on the web channel = chunks on the output stream).
  `MessageSentEvent` demotes to pure observability (other observers), no longer
  the web's delivery path.
- **Deferred on web**: persisted batches + marks work like IM (the suspender
  path needs no web special case).
- **dev parity**: web messages exercise the real dispatch (triage included) —
  what you debug on web is what runs on IM.

## Decisions to settle
1. **Protocol conversion locus** — (a) timeline/feelers emit Vercel chunks
   directly (fewest hops), or (b) they emit octomate-semantic events and the API
   edge converts (protocol-swappable, UI protocol stays at the boundary).
   Lean (a) for simplicity; revisit if a second web protocol ever appears.
2. **consume() vs override** — reuse `drive_timeline` + hooks (uniform, loses raw
   part indexes — acceptable, chunks re-key parts anyway) vs a `consume()`
   override pumping raw events (closer to today's UoW-12 stream, but bypasses
   the renderer hierarchy). Lean hooks — it is the uniformity this plan exists for.
3. **Triage on web** — full kick→triage parity (lean: yes, parity is the point;
   latency acceptable for a dev surface) vs a straight-to-reception channel
   config.
4. **Stream registry lifecycle** — per-run streams keyed by conversation;
   behavior when the browser disconnects mid-run (run continues, chunks dropped,
   transcript/model messages still record — replay covers the gap) vs buffering.

## Risks
- 🔴 **Request-scoped streaming vs fire-and-forget dispatch.** The HTTP response
  must stream exactly this run's chunks while `kick` runs concurrently;
  registration/teardown of the output stream (and the no-subscriber case) is the
  hard 20%.
- 🟡 **ABC fit.** Some `Ink` members are platform-API-shaped; trivial dev
  implementations are fine but should stay honest (fail-fast where genuinely
  meaningless, e.g. media download).
- 🟢 The renderer side is low-risk: the hook→chunk mapping is mechanical and the
  chunk vocabulary already exists (UoW-12).

## Sequencing

Reconnect replay synthesizes from stored model messages, so this plan does
**not** hard-depend on [conversation-transcript.md](conversation-transcript.md)
(the transcript backs the messages-only view + compaction survival). Ideally
lands alongside/after [send-toolset.md](send-toolset.md), whose web special
cases this deletes — if the send capability lands first, its channel-less branch
is simply removed when this lands.

## Verification (when implemented)
- WebTimelineState: hook calls → expected chunk sequences (mirror the
  test_dev_ui_stream cases).
- End-to-end: POST message → SSE chunks for a scripted run (FunctionModel),
  including a deferred round-trip via the web feelers.
- Replay: transcript + model-dump composite renders a finished conversation.
- Gates: pytest / ruff / CLI pyright.
