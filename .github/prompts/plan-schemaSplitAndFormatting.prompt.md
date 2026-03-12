# Plan: Schema split + LLM-readable message formatting

## TL;DR

Split the monolithic `schemas/events.py` into 4 focused modules, add `__str__` methods to segments and events so an LLM agent gets a single readable line per message (markdown-like format), and keep CQ code / IM-specific details at the tentacle layer.

## Steps

### Phase 1: Schema module split

1. Create `schemas/session.py` — Move `SessionKey`, `Sender`, `Anonymous` here. No external deps.
2. Create `schemas/segments.py` — Move all `*Data` TypedDicts, `Segment` base, all `*Segment` classes, and `MessageSegment` union here. Imports only from pydantic/typing.
3. Create `schemas/actions.py` — Move `ActionResponse`, `SendGroupMsgParams`, `SendGroupMsgAction`, `SendPrivateMsgParams`, `SendPrivateMsgAction`, `CallApiAction` here. Imports `MessageSegment` from `segments`, `SessionKey` from `session`.
4. Keep `schemas/events.py` — Retains `OneBotEvent`, `MessageEvent`, `GroupMessageEvent`, `PrivateMessageEvent`, all notice events, request events, meta events, and their unions (`MessageEventUnion`, `NoticeEventUnion`, `RequestEventUnion`, `MetaEventUnion`, `OneBotEventUnion`). Imports `SessionKey` from `session`, `MessageSegment`/segment types from `segments`, `Sender`/`Anonymous` from `session`.
5. Update `schemas/adaptors.py` — Fix imports to point to new modules (`actions.py` for action types, `events.py` for `OneBotEvent`/`OneBotEventUnion`/`ActionResponse`).
6. Update `schemas/__init__.py` — Re-export everything from all 4 modules + adaptors. Keep `__all__` aligned.
7. Update consumer imports in `nerve.py`, `base.py`, `tentacles/base.py`, `tentacles/napcat.py` — All currently import from `schemas.events`; update to import from the correct new submodule (or just keep importing from `schemas/__init__` if they use it).

### Phase 2: Segment `__str__` methods (markdown-like)

Add `__str__` to each segment class for LLM-readable output:

**Straightforward segments (implement now):**
| Segment | `__str__` output |
|---------|-----------------|
| `TextSegment` | raw text (no wrapping) |
| `AtSegment` | `@{name}` if name present, else `@{qq}` |
| `ImageSegment` | `[image: {summary or name or "image"}]` |
| `ReplySegment` | `[reply: {id}]` |
| `FaceSegment` | `[face: {id}]` |
| `RecordSegment` | `[audio]` |
| `VideoSegment` | `[video]` |
| `ShareSegment` | `[share: {title} {url}]` |
| `LocationSegment` | `[location: {title or "({lat},{lon})"}]` |
| `ForwardSegment` | `[forward: {id}]` |
| `JsonSegment` | `[json]` |
| `XmlSegment` | `[xml]` |

**Skipped / placeholder for now (tricky segments):**
| Segment | Why skipped | Placeholder |
|---------|------------|-------------|
| `RpsSegment` | Game mechanic, no text value | `[rps]` |
| `DiceSegment` | Game mechanic | `[dice]` |
| `ShakeSegment` | UI effect only | `[shake]` |
| `PokeSegment` | UI effect only | `[poke]` |
| `AnonymousSegment` | Metadata flag, not content | `[anonymous]` |
| `ContactSegment` | Recommend friend/group, complex | `[contact: {type}:{id}]` |
| `MusicSegment` | Rich embed | `[music: {title or type}]` |
| `NodeSegment` | Nested forwarded msg | `[node]` |

### Phase 3: MessageEvent `__str__`

8. Add `__str__` to `MessageEvent` base: `"{display_name}({user_id}): {segments_joined}"` — calls `str(seg)` on each segment and joins them.
9. Override in `GroupMessageEvent`: `"[group:{group_id}] {display_name}({user_id}): {segments_joined}"`
10. `PrivateMessageEvent` inherits base `__str__` (private context is implicit).

The `display_name` property already exists on `GroupMessageEvent` (card or nickname) and `PrivateMessageEvent` (nickname). Move `display_name` to `MessageEvent` base.

### Phase 4: CQ code strategy (design note, no code)

CQ codes (`[CQ:type,key=val,...]`) are QQ/OneBot-specific. The approach:

- **Tentacle layer translates**: napcat tentacle parses JSON segments from OneBot, stamps `tentacle_id`. No CQ string parsing needed (napcat sends JSON, not CQ strings).
- **Internal representation**: Our Pydantic segment models are already IM-agnostic in structure. The `__str__` output uses markdown-like syntax, not CQ codes.
- **Future tentacles** (Discord, Telegram, Slack, WeChat): Each will map their native message format to/from the same `Segment` models. If a segment type doesn't exist, it falls back to `[unsupported: {type}]`.
- **No CQ code parser needed** unless we add an HTTP-POST tentacle that receives CQ strings (unlikely).

## Relevant files

- `octomate/schemas/events.py` — Source file to split (currently ~615 lines)
- `octomate/schemas/adaptors.py` — Update imports
- `octomate/schemas/__init__.py` — Update re-exports
- `octomate/nerve.py` — Imports `MessageEvent`, `OneBotEventUnion`, `SessionKey`
- `octomate/base.py` — Imports segments, actions, `SessionKey`
- `octomate/tentacles/base.py` — Imports `SessionKey`, `ActionUnion`, `MessageEvent`
- `octomate/tentacles/napcat.py` — Imports `ActionResponse`, `inbound_adapter`

## Verification

1. Run `uv run python -c "from octomate.schemas import *; print('ok')"` — all re-exports work
2. Run `uv run python -c "from octomate.schemas.segments import TextSegment; print(str(TextSegment(data={'text': 'hello'})))"` — outputs `hello`
3. Run `uv run python -c "from octomate.schemas.segments import AtSegment; print(str(AtSegment(data={'qq': '123', 'name': 'Alice'})))"` — outputs `@Alice`
4. Run `uv run python -c "from octomate.schemas.segments import ImageSegment; print(str(ImageSegment(data=ImageData(file='http://example.com/cat.jpg', summary='cute cat'))))"` — outputs `[image: cute cat]`
5. Run the app via launch config — verify no import errors, messages still flow
6. Type-check with pyright/mypy if available

## Decisions

- Markdown-like format for `__str__` (per user choice)
- 4-module split: `session.py`, `segments.py`, `actions.py`, `events.py` (per user choice)
- CQ codes stay at tentacle layer — no internal CQ parser
- `display_name` property to be added/moved to `MessageEvent` base
- Tricky segments get simple placeholder strings for now

## Further Considerations

1. Should `Segment` base class get a fallback `__str__` returning `[{type}]`? **Recommended: yes**, so any future segment type is covered.
2. Should we add `__repr__` too for debugging (showing full data)? Default pydantic `__repr__` may suffice.
