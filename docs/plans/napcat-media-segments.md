# Plan: NapCat native media — outbound image/file segments

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-12
> **Builds on:** [send-toolset.md](send-toolset.md) (`ChannelTentacle.send_segments` —
> with [web-channel.md](web-channel.md) it becomes the universal per-channel send
> surface: Slack / Lark / NapCat / Web, and this plan is NapCat's override)
> · **Reference:** the legacy implementation on the `archive` branch already did
> this — port its approach, not its architecture.

## TL;DR

NapCat (OneBot) renders everything as flattened plain text today: outbound is
`chromo.outbound_markdown` (markdown → stripped text), and image/file segments
degrade to `str(segment)` placeholders. But **OneBot's wire format natively *is*
octomate's segment shape** (`{"type": "image", "data": {"file": …}}`), so NapCat
is the one platform where rich outbound is nearly free. Give it:

1. an outbound **segments → OneBot message** conversion on `NapcatChromo`,
2. outbound **media preparation** (local file → `base64://…`) on the send path,
3. a `send_segments` override on `NapcatTentacle` so the send tool (and later the
   reply path) delivers native images/files instead of text placeholders.

## What the archive branch already proved (take a ref)

| Legacy piece (`archive` branch) | What it did | Port target |
|---|---|---|
| `napcat/chromo.py` `squirt(segments, reply_to)` | segments serialized **directly** to the OneBot message array via `model_dump_json` (octomate's `{"type","data"}` shape is OneBot's); `MarkdownSegment`/`TextSegment` flattened through `strip_markdown` (still in today's [utils.py](../../octomate/utils.py)) | `NapcatChromo.outbound_segments(segments) -> list[NapcatOutboundMessage]` |
| `napcat/base.py` `secrete(seg)` | outbound media prep: read the local `data.path`, rewrite `data.file` to `base64://<b64>` so NapCat uploads inline — no separate upload API needed | the same prep inside the outbound conversion (images **and** files) |
| `napcat/base.py` `send_platform_message` | OneBot `send_group_msg` / `send_private_msg` actions carrying the segment array + a `reply` param | today's `NapcatInk.send_message` already sends `NapcatOutboundMessage.segments` — verify it posts the array unmodified; the `reply` param matters later for [reply-and-targeting.md](reply-and-targeting.md) |
| `napcat/ink.py` `get_image_url` / `download` | inbound media (already ported) | — |

The legacy architecture (agent-coupled `squirt`, `twitch`) stays dead; only the
conversion + `base64://` preparation port.

## Design sketch

1. **`NapcatChromo.outbound_segments(segments: list[OutputSegment])`** —
   text/markdown → OneBot text segments (via `strip_markdown`); image/file →
   OneBot `image`/`file` segments with `data.file` rewritten to `base64://…`
   from the local path (fail-fast on missing files); card → text fallback
   (`str(segment)`) — OneBot has no card.
2. **`NapcatTentacle.send_segments` override** — convert via chromo, send via
   `ink.send_message`. (The Default base keeps the text-join fallback for any
   future transport-less channel.)
3. **Reply path (optional same slice):** the non-streaming reception fallback
   renders segment replies through `markdown_from_output` (text). Upgrading it to
   `send_segments` for NapCat would make *replies* media-native too — decide at
   implementation whether that lands here or stays text.

## Out of scope
- OneBot `reply` segment / reply threading — [reply-and-targeting.md](reply-and-targeting.md).
- Audio/video/record segment kinds; group-file upload APIs beyond the inline
  `base64://` path.

## Verification
- Chromo unit tests: segment list → OneBot array (text flattening, base64
  rewriting, card fallback; missing file raises).
- `send_segments` via a fake NapCat ink (captured action payloads).
- Gates: pytest / ruff / CLI pyright, no new errors.
