# Plan: the send capability (`send_message` — messages / images / files)

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-12
> **Builds on:** [agent-event-stream.md](agent-event-stream.md) (segment renderers +
> upload primitives from UoW-11; the `TodoCapability` pattern from UoW-4).
> Supersedes that plan's "UoW-14 emission tools" sketch — this is its own plan.
> **Related:** [conversation-transcript.md](conversation-transcript.md) (the record
> hop), [web-channel.md](web-channel.md) (makes web a real channel — uniform
> delivery), [napcat-media-segments.md](napcat-media-segments.md) (native NapCat
> media), [reply-and-targeting.md](reply-and-targeting.md) (reply-to + targeting).

## TL;DR

Give the agent **one tool to send content mid-run**:

```python
async def send_message(ctx: RunContext[Any], segments: list[OutputSegment]) -> str
```

wrapped in a **`SendCapability` following the `TodoCapability` pattern** —
agent-level, scoped per run via `ctx.conversation_id`. The segments vocabulary is
the same one the reply already uses (markdown / text / image / card / **file**,
added here), so "messages, images, files" are one tool, not three. The agent
decides **what** to send; the system decides **where**: `ctx.conversation_id`
resolves to the persisted `Conversation` (which carries the full key +
`channel_tentacle_id`), and the send goes to that conversation. The tool exposes
**no channel information** — no platform names, ids, or keys in args, schema, or
returns. Choosing a *different* destination is outbound targeting
([reply-and-targeting.md](reply-and-targeting.md)): an extra argument later,
nothing else changes here.

Delivery is the tool's own synchronous side effect, **and** the tool emits a
`MessageSentEvent` onto the stream (the todo metadata template) as the
*observation record*: channel timelines deliberately ignore it (the message
already landed in that very conversation — and the `send_message` tool-call
itself is **skipped in timeline rendering**, the `ask_questions` precedent);
non-channel observers (run archive, logging, the dev_ui shim until
[web-channel.md](web-channel.md) lands) render it.

## Reviewed: direct send vs. emitted event → the hybrid

Three shapes were reviewed:

**(A) The tool sends directly**; its tool-call event joins the timeline skip set.
**(B) The tool only emits a display event**; `consume()` renders it through the
existing `answer_segment` feeler path.
**(A+) Hybrid — chosen:** the tool sends directly *and* emits the event as an
observation record.

| | (A) direct send | (B) emitted event | (A+) hybrid |
|---|---|---|---|
| Non-streaming runs (NapCat reception) | delivered | **lost** — no consumer | delivered (event is only the record) |
| Delivery feedback to the model | `"sent"` / real tool error | none — tool returns before rendering; render failure trips the timeline failed-flag silently | `"sent"` / real tool error |
| IM rendering | the sent message itself (tool-call skipped) | via `answer_segment` (streaming only) | the sent message itself (event ignored, tool-call skipped) |
| dev_ui / other observers | tool-call JSON only | renders the event | renders the event as message content |
| Machinery | channel send surface + skip entry | event + consume case + dev_ui chunk | both — but the event side is the proven todo template |

**Why the hybrid.** (B) alone fails the feature's contract — *a send tool whose
sends can be lost isn't a send tool* (non-streaming delivery, model-visible
failure). (A) alone leaves observers blind: dev_ui would show tool-call JSON, and
nothing else on the stream says "this content went out". The split that resolves
it: **direct is for delivery, the event is for live observation**.

*Web evolution:* before [web-channel.md](web-channel.md), a web conversation has
no channel, so the event doubles as the web's delivery. Once the web **is** a
channel, delivery is uniform (`send_segments` everywhere — the web channel emits
chunks onto its output stream) and the event demotes to **pure observability**
on every surface. The tool body doesn't change either way.

The full shape is **record → deliver → announce** (see §1a): the tool also
appends the sent message to the **conversation transcript**
([conversation-transcript.md](conversation-transcript.md)) — the durable
user-facing copy that the future history-search toolset and the messages-only
conversation view read. (Full-fidelity web replay re-synthesizes the event
stream from stored model messages instead — the `MessageSentEvent` stashed on
`ToolReturn.metadata` replays sends in-position; see the transcript plan's
replay section.)

## Design

### 1. `SendCapability` — the todo pattern, complete

```python
@dataclass
class SendCapability(AbstractCapability[Any]):
    """One tool to send segments to the run's conversation, resolved from
    ctx.conversation_id — the agent never sees channel information."""

    channels: dict[str, ChannelTentacle]            # the live Octomate registry
    conversation_manager: ConversationManager
    toolset: FunctionToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None: ...            # build the send_message toolset
    def get_toolset(self) -> AbstractToolset[Any] | None: ...
    def get_instructions(self) -> AgentInstructions[Any] | None: ...
    async def wrap_run_event_stream(self, ctx, *, stream): ...
        # forwards MessageSentEvent stashed on ToolReturn.metadata — verbatim
        # the TodoCapability mechanism.
```

### 1a. The tool body — record, deliver, announce

```python
async def send_message(ctx: RunContext[Any], segments: list[OutputSegment]) -> ToolReturn:
    """Send messages to the current conversation immediately, without ending
    your turn — progress updates, intermediate results, an image or a file.
    Anything you send here is already delivered: do NOT repeat it in your
    final reply."""
    conversation = await conversation_manager.get(conversation_id(ctx))
    # 1. RECORD — the durable user-facing copy (history search + conversation view).
    await transcripts.record_assistant(conversation, segments)
    # 2. DELIVER — synchronous, so non-streaming runs deliver and the model sees
    #    real success/failure. A channel-less conversation (web UI) skips this
    #    hop: the stream event below IS its delivery.
    channel = channels.get(conversation.channel_tentacle_id)
    if channel is not None:
        await channel.send_segments(conversation.key, segments)
    # 3. ANNOUNCE — the live observation record on the run's event stream.
    return ToolReturn(
        return_value="sent",
        metadata=[MessageSentEvent(segments=segments)],
    )
```

- `conversation_id(ctx)` is the same fail-fast helper the todo tools use.
- **New small manager method:** `ConversationManager.get(conversation_id)` —
  load-by-id (cache-aware); today only `ensure(key)` exists.
- The transcript hop is [conversation-transcript.md](conversation-transcript.md);
  if this capability lands first, step 1 is a stub slot, not a blocker.
- A **channel-less conversation** (e.g. `channel_tentacle_id="dev_ui"`) is a
  valid case, not an error: record + announce still happen, and the web UI
  receives the send via the stream (live) and the transcript (replay). *(This
  branch is deleted once [web-channel.md](web-channel.md) lands — the web then
  has a real channel and the deliver hop is uniform.)*
- Delivery failures (missing file, upload error) raise — real tool errors the
  model sees; no silent fallback.
- Wired as an agent default next to `TodoCapability` wherever the channels
  registry is in reach (Decisions 1); like todos, the *same agent* serves triage
  runs, so the tool is technically available there too — accepted, instructions
  steer usage.

### 2. `MessageSentEvent` — the observation record

A `DisplayEvent` subclass in [capabilities/events.py](../../octomate/capabilities/events.py):

```python
class MessageSentEvent(DisplayEvent):
    event_kind: Literal["message_sent"] = "message_sent"
    segments: list[OutputSegment]
```

Consumers:
- **Channel timelines (`drive_timeline`)**: explicit case → ignore (with a
  comment: the content was already delivered to this conversation by the tool
  itself; rendering it would duplicate). Once [web-channel.md](web-channel.md)
  lands, this covers the web too.
- **dev_ui (`OctomateUIEventStream`) — transitional**: render as a transient
  `data-message-sent` chunk (segments payload) — message content instead of
  tool-call JSON. Retires with the shim when the web channel lands (the web
  timeline then ignores the event like every channel).
- Pure observers (run archive, logging) get it for free — the event's long-term
  reason to exist.
- Non-streaming runs lose only the record, never the delivery.

### 3. `ChannelTentacle.send_segments` — the one-shot per-platform send surface

The timeline's segment renderers (`answer_segment`) live on the per-run streaming
state and can't be called outside a run. Lift the same per-platform logic into a
one-shot channel method:

```python
async def send_segments(
    self, key: ConversationKey, segments: list[OutputSegment]
) -> None:
    """Send completed segments to the conversation, platform-natively."""
```

- **Base/Default (NapCat for now):** text/markdown join (`str(segment)`,
  `"\n\n"`-separated) through `present_markdown`; image/file render their text
  placeholder. *(Native NapCat media is
  [napcat-media-segments.md](napcat-media-segments.md).)*
- **Slack:** text/markdown → `feelers.markdown.present`; image →
  `ink.upload_image` (thread-shared `files_upload_v2`) with
  `chromo.thread_context(key)`; file → the same `files_upload_v2` path
  (share/generalize the primitive, don't copy it).
- **Lark:** text/markdown → `feelers.markdown.present`; image →
  `ink.upload_media` → `msg_type="image"`; file → **one new ink primitive**
  `LarkInk.upload_file` (`im.v1.file.acreate`, verified available) →
  `msg_type="file"`.
- **Web ([web-channel.md](web-channel.md), when it lands):** emit message-content
  chunks onto the conversation's output stream — `send_segments` becomes the
  universal per-channel send surface (Slack / Lark / NapCat / Web).

If the duplication with the timeline `answer_segment` logic itches later, the
timeline can delegate to shared helpers — **not in this plan**.

### 4. `FileSegment` joins `OutputSegment`

`Text | Markdown | Image | Card | File` — ticking one item off the TODO in
[segments.py](../../octomate/schemas/segments.py). The timeline `answer_segment`
renderers keep treating files as text placeholders (the send tool is the primary
file path); upgrading the reply path is a trivial follow-up.

### 5. Timeline skip — the message renders itself

Add `"send_message"` to the skip set ([feelers/output.py](../../octomate/tentacles/channel/feelers/output.py)
`SKIPPED_PLAN_TOOL_NAMES`, used by `drive_timeline` and the platform states; rename
to reflect the broadened meaning, e.g. `SKIPPED_TIMELINE_TOOL_NAMES`). The sent
message lands in the thread at its natural position; no task card narrates the
call.

### 6. Prompt adjustment — no duplicate delivery

The double-delivery failure mode (model `send_message`s content *and* repeats it
in the final reply, so the user reads it twice) is countered in **three layers**:

1. **Tool docstring** (the schema the model always sees): "anything you send here
   is already delivered: do NOT repeat it in your final reply."
2. **Capability instructions** (`get_instructions`, the todo pattern):
   - use `send_message` for content the user should see *before* the run ends —
     progress, intermediate artifacts, images/files produced along the way;
   - the final structured reply continues from what was already sent — summarize
     or extend, never restate it;
   - if everything worth saying was already sent, return a minimal closing reply
     (e.g. a one-line wrap-up) rather than re-sending content.
3. **`SYSTEM_PROMPT` output-format note** (one bullet): the reply is what ends the
   turn; `send_message` output has already reached the user and must not be
   duplicated by the reply.

Verification includes a prompt-level regression watch (manual, real runs) since
this is behavioral, not mechanical.

## Decisions to settle
1. **Where the capability is constructed.** It needs the live `channels` registry
   + `ConversationManager`. Recommended: assembly-site wiring (where `Octomate`
   is in reach), e.g. optional params on `build_inkling_agent`; assemblies that
   pass nothing don't get the tool. Alternative: append at tentacle registration.
2. **Skip-set rename** — `SKIPPED_PLAN_TOOL_NAMES` → `SKIPPED_TIMELINE_TOOL_NAMES`
   (it no longer only skips plan tools). Recommend yes; three references.
3. **Transcript ordering** — land the transcript store first (cleanest), or land
   the capability with the record hop stubbed and wire it when
   [conversation-transcript.md](conversation-transcript.md) lands. Either works;
   the tool body is shaped for it.
4. **Web-channel ordering** — if [web-channel.md](web-channel.md) lands first,
   the channel-less branch and the dev_ui `data-message-sent` chunk are never
   written; if this lands first, both are written as marked-transitional and
   deleted by the web-channel plan. No interface changes either way.

*(Resolved: channel-less conversations like dev_ui are a valid case — record +
announce without the deliver hop; no error. See §1a. Transitional: the case
exists only until [web-channel.md](web-channel.md) gives the web a real channel.)*

## Risks
- 🟡 **Double delivery.** Behavioral; mitigated by the three prompt layers (§6),
  watched in real runs.
- 🟡 **Slack upload dedupe.** `files_upload_v2` now backs markdown overflow,
  images, and files — keep one parameterized primitive.
- 🟢 **Plumbing.** No new generics; capability/toolset/metadata-event paths are
  each proven (todos, inkling toolset).

## Verification
- `send_segments` per platform via the existing fake inks (Slack upload + file +
  markdown; Lark image/file message + interactive; Default joined markdown).
- Capability: a FunctionModel run scripting a `send_message` tool call —
  conversation resolved from `ctx.conversation_id` (fake manager), segments land
  on a fake channel, `"sent"` returned, `MessageSentEvent` observed on the
  stream; tool schema contains no channel fields.
- Timeline: a `send_message` tool call/result pair renders no task chunk (Slack) /
  no card (Lark); a `MessageSentEvent` renders nothing on channel timelines.
- dev_ui (transitional, until [web-channel.md](web-channel.md)):
  `MessageSentEvent` → `data-message-sent` chunk.
- Gates: `.venv/bin/python -m pytest -q`, `ruff`, CLI `pyright` (no new errors).

## Out of scope (later)
- **Reply-to + outbound targeting** — [reply-and-targeting.md](reply-and-targeting.md).
- **NapCat-native image/file segments** — [napcat-media-segments.md](napcat-media-segments.md).
- Edit/delete tools (need message-id handles); audio/video media kinds.
