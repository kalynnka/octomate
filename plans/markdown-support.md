# Markdown Support + Acknowledge Tool

## Context

Two problems to solve:
1. Agent output contains markdown that platforms may or may not render — need per-platform handling
2. User gets no feedback during long tool calls (weather, pixiv, etc.) — need an acknowledgment mechanism

## Architecture Decisions

**Message sending:** Hybrid — keep `list[AgentMessage]` structured output as primary channel. Add an `acknowledge(text)` tool for immediate feedback during long operations.

**Markdown rendering:** Agent always uses markdown freely. Tentacles that support it (Lark) render natively. Tentacles that don't (NapCat) strip it to plain text. Stripping cost is low — only tables degrade badly, everything else (bold, lists, code, links) reads fine as plain text.

**Cards:** Tentacle decides. Agent stays platform-agnostic. Lark detects markdown and wraps in interactive card; plain messages stay as `msg_type: "text"`.

## Changes

### 1. Add `strip_markdown()` and `has_markdown()` — `octomate/utils.py`

**`has_markdown(text: str) -> bool`** — regex detection of markdown syntax: `**bold**`, `*italic*`, `` `code` ``, code fences, `# headers`, `[links](url)`, `> blockquotes`, `~~strikethrough~~`.

**`strip_markdown(text: str) -> str`** — `re.sub()` chain:
- `**bold**` / `__bold__` → `bold`
- `*italic*` → `italic`
- `` `code` `` → `code`
- `[text](url)` → `text (url)`
- `# Header` → `Header`
- `> blockquote` → content preserved
- Code fences → removed, content preserved
- `~~strike~~` → `strike`
- List markers (`- `, `1. `) → preserved as-is

No external dependencies.

### 2. Add `acknowledge` tool — `octomate/agents/mind.py`

Register a tool on the agent that sends an immediate message to the user:

```python
@agent.tool
async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
    """Send a quick message to the user before doing heavy work."""
    # Access tentacle via session_key, build and send a single TextSegment message
    ...
    return "acknowledged"
```

Implementation: `SessionContext` needs a reference to the octopus (or a send callback) so the tool can dispatch a message. Add a `send` callback field to `SessionContext`:

```python
@dataclass
class SessionContext:
    session_key: SessionKey
    active_skills: set[str] = field(default_factory=set)
    send: Callable[[list[AgentSegment]], Awaitable[None]] | None = None
```

In `octopus.think()`, bind the callback before `agent.run()`:

```python
async def _send(segments):
    # build action from session key, dispatch to tentacle
    ...

deps = SessionContext(session_key=key, send=_send)
```

The acknowledge tool calls `ctx.deps.send([TextSegment(data={"text": text})])`.

### 3. Rewrite `LarkTentacle.splash()` — `octomate/tentacles/lark.py`

**Current:** Sends `msg_type: "text"` with `{"text": "..."}`.

**New — detect markdown first:**
- Collect text/at parts into joined string, call `has_markdown(text)`
- **No markdown:** send as `msg_type: "text"` (current behavior, no card)
- **Has markdown:** send as `msg_type: "interactive"` with Lark card schema v2:
  ```json
  {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "..."}]}}
  ```

Card mode details:
- Accumulate text/at into markdown string, flush as `{"tag": "markdown"}` element when image encountered
- `AtSegment` → `<at id=user_id></at>` (card syntax)
- `ImageSegment` → upload, add `{"tag": "img", "img_key": key}` element
- Image-only messages → send as individual `msg_type: "image"` (no card)
- Reply: `reply_message()` with appropriate msg_type based on markdown detection

Non-card mode: existing behavior unchanged.

### 4. Update `NapcatTentacle.splash()` — `octomate/tentacles/napcat.py`

Add markdown stripping at the start of `splash()`:
```python
for seg in segments:
    if isinstance(seg, TextSegment):
        seg.data["text"] = strip_markdown(seg.data["text"])
```

Rest of method unchanged.

### 5. Update `SYSTEM_PROMPT` — `octomate/agents/mind.py`

- Tell agent it can use markdown in text segments (bold, italic, code, lists, links, blockquotes)
- Guidance: keep formatting light and natural for chat
- Tell agent about `acknowledge` tool: use it before calling tools that take time, so the user knows you're working on it

**Done last** so tentacles are ready before agent starts outputting markdown.

## Files to Modify

| File | Change |
|------|--------|
| `octomate/utils.py` | Add `strip_markdown()` and `has_markdown()` |
| `octomate/agents/mind.py` | Add `acknowledge` tool, update `SessionContext`, update `SYSTEM_PROMPT` |
| `octomate/octopus.py` | Bind `send` callback on `SessionContext` before `agent.run()` |
| `octomate/tentacles/lark.py` | Rewrite `splash()` — detect markdown, use cards when needed |
| `octomate/tentacles/napcat.py` | Add `strip_markdown()` call in `splash()` |

## No schema changes needed

Markdown lives in `TextSegment.data.text`. Acknowledge uses existing segment types. No new segment types.

## Verification

1. Ask the bot about weather on Lark → should see an ack message, then a card with formatted forecast
2. Simple greeting on Lark → plain text, no card
3. Same weather question on QQ → ack message, then clean plain text forecast (no raw markdown)
4. Test: image-only messages, at-mentions mixed with markdown, reply messages
