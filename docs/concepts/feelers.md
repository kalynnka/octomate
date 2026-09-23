# Feelers

A channel is three things, kept apart on purpose:

- A **Chromo** translates. Platform payloads in, core schemas out, and markdown or
  segments back into platform payloads. It sends nothing.
- An **Ink** transports. It holds the platform client, sends, edits, uploads,
  downloads and opens direct messages. It decides nothing about content.
- **Feelers** render. They decide how a run is drawn on this platform and how the
  cards a person answers look and read back.

If you want the MVC analogy: the model is the run's event stream, the segments, the
ledger and the persisted actions; the feelers are the view; and the controller is
the reflex `React` node on the way out and the channel's `ingest` on the way in.
The chromo is a codec, not a controller.

## The six feelers

Every channel instance carries one of each:

| Feeler | Draws |
|---|---|
| `timeline` | A streaming run: thinking, tool calls, answer text, todos, subagents, mid-run notices |
| `markdown` | A finished reply, chunked and sent |
| `segments` | A reply made of segments: text, mentions, images, files, cards |
| `approvals` | Approval cards for a batch |
| `ask_questions` | Question cards for a batch |
| `oauth` | Authorisation and profile-link messages, always to a private surface |

A channel gets working defaults for all six out of its ink and chromo: a timeline
that accumulates and sends one message at the end, plain-text approval and question
cards, a plain-text OAuth message. Slack, Lark and Discord replace nearly all of
them; QQ replaces two.

The OAuth feeler's `present` is concrete on the base class and resolves the private
address itself, redirecting a request made on a shared surface to the person's DM
and raising when the platform has nowhere private. A subclass fills in `send` and
never gets to pick where it sends.

## The timeline pipeline

`feelers/output.py` is the shared machinery every streaming channel builds on:

1. **Chunking.** A `MarkdownChunker` splits at the best boundary under a platform's
   limit: paragraph, line, sentence, whitespace. Discord instantiates it at 2000.
2. **Batching.** A `TextStreamBatcher` buffers token deltas per block and decides
   when to flush from the channel's `stream` settings: always on the first piece,
   at `max_chars`, never under `min_chars`, otherwise on `flush_interval`.
3. **Flushing.** A `StreamFlusher` runs one background task so the drive loop never
   blocks on a slow platform call; deltas arriving mid-flush coalesce.
4. **Dispatch.** `TimelineState.drive` is one `match` over the run stream, mapping
   each event to a hook: `thinking_start`, `thinking_delta`, `thinking_end`,
   `answer_*`, `tool_start`, `tool_end`, `todo`, `message_sent`,
   `oauth_authorization`, `present_actions`. The base hooks draw nothing; a channel
   overrides the ones it can draw. Draining the whole stream is mandatory, a render
   failure is logged and the loop keeps draining, and a final output that never
   streamed is rendered once as a fallback.
5. **Notices.** A rule every override honours: a hook that renders reply text marks
   the surface as noticed, and every hook that opens a new entry (a thinking block,
   a tool call, a todo, never a tool result) calls `begin_entry` first, which
   rotates the surface once if a notice streamed in between. That is what lets a
   mid-run message land between two tool cards instead of inside one.
6. **Subagents.** A commission opens a child timeline keyed by its tool call; its
   result appends the response and settles it.

Tools the timeline never draws: `ask_questions`, `send`, `commission`, `whisper`,
`teleport` and the output tool. They are plumbing, not work.

## Cards and how they come back

Presenting a batch creates it, presents approvals then questions, and records each
card's platform message id against its action, so the card and the row stay linked.
Answering is per channel, and two designs coexist:

- **Slack and Lark** carry the batch's state in the button payload: ids, the
  actions, the page, and the answers so far. A press validates the payload and
  kicks the graph when the batch is complete.
- **Discord** carries ids only, in the button's custom id, and reloads the batch
  from the database under a per-batch lock. Approvals survive a restart; answers
  typed but not submitted do not.

Either way the channel builds one `DeferredActionBatchResponse` and kicks. A batch
already resolved is refused, never resumed twice.

## Where a reply lands

Both the markdown feeler and the timeline compute the destination from the
address: the platform thread when there is one, the chat otherwise, with a reply
target when a reply segment led the message. Each ink then maps that onto its
platform's own idea of a thread, which is where the per-platform differences on
[Threads and chat rooms](../usage/threads.md) come from.
