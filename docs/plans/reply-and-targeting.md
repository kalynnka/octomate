# Plan: reply-to segments + outbound target switching (goals)

> **Status:** goal only — no design yet · **Owner:** @luhui · **Created:** 2026-06-12
> **Builds on:** [send-toolset.md](send-toolset.md),
> [conversation-transcript.md](conversation-transcript.md) (message handles),
> [web-channel.md](web-channel.md) (web conversations as ordinary
> reply/targeting surfaces) ·
> **Absorbs:** [agent-event-stream.md](agent-event-stream.md) UoW-15 (outbound targeting).

Two related goals, parked here until the send capability has landed and proven
itself. Deliberately *no* detailed solution yet — both need a design pass first.

## Goal 1 — reply-to as a first-class outbound segment

The agent can address a **specific message**: `ReplySegment` becomes usable in
both the **send tool** (`send_message` segments) and the **structured reply**
(`OutputSegment`), so a response can quote/thread onto the message it answers —
e.g. answering one question out of three in a busy group chat.

What this implies (to be designed, not decided here):
- The agent needs **message-id handles** for inbound messages (they exist on
  `MessageEvent`/`ReplySegment` inbound; how they surface to the model — history
  rendering? context header? — is the design question).
- The strongest handle candidate: the **transcript entry id**
  ([conversation-transcript.md](conversation-transcript.md)) — platform-agnostic
  (web messages have no platform id at all, so once
  [web-channel.md](web-channel.md) makes web an ordinary reply surface, a
  platform-id handle can't cover it), opaque to the model, and resolvable
  server-side to the platform id recorded on the entry.
- Per-platform reply semantics differ: OneBot `reply` segment / `reply` param
  (the legacy `archive` branch already sent it), Lark `reply_message` /
  `reply_in_thread` (exists in `LarkInk`), Slack `thread_ts` (a reply *is* a
  thread post — partially in place already), Web (a quote/reference rendered by
  the UI from the handle).
- `OutputSegment` gains `ReplySegment` (one more tick on the segments TODO), with
  the rule the inbound docstring already states: reply must be the first segment.

## Goal 2 — outbound target switching

The agent can direct a send to a **different conversation than the run's own** —
"send the summary to the ops channel", "send it to me on Lark".

Constraints already settled by the parent plan (§8) and carried forward:
- The agent **never constructs or sees raw addresses** (no platform ids, chat
  ids, keys). It picks a target **by opaque id from a system-offered,
  pre-resolved candidate set** — the same pattern triage's `candidates` +
  `decision.target_id` already uses.
- The **system owns identity resolution + authorization**. "Me on Lark" requires
  a cross-platform identity mapping that does not exist yet (its own
  schema/manager — a real prerequisite, and the reason the parent plan rated
  this 🔴).
- Mechanically, this should be no more than an optional `target` argument on the
  existing `send_message` tool (and possibly on the reply), resolved server-side
  — the send-toolset plan was shaped so nothing else changes.

With [web-channel.md](web-channel.md), web conversations become ordinary
candidates too — "send it to my web chat" needs no special path, just an entry
in the offered set; conversely a web-originated run can target IM channels
through the same mechanism.

Open questions for the future design pass: how broad the offered candidate set is
(origin only / configured channels / everywhere the user's identity is known),
where candidates are computed (triage vs capability), and how reply-to interacts
with cross-channel targets (a reply handle is only meaningful on its own channel —
another argument for transcript-id handles, which carry their conversation).
