# Claude Agent Integration — Implementation Plan

**Status:** approved design, not yet started
**Scope:** integrate the Claude Agent SDK (`claude-agent-sdk`, installed `0.1.80`; CLI `2.1.177`) into octomate in two ways.

- **Pattern 1 — routable Claude agent tentacle (build now).** Triage dispatches "let Claude do X" to a Claude agent that runs the SDK locally or on a remote host over SSH, and streams adapted events back to the originating channel.
- **Pattern 2 — mirror native runs → web replay (overview only, build later).** A user runs Claude natively (app / VS Code / CLI); hooks + the transcript JSONL are ingested into octomate so the run can be replayed in web. Reuses Pattern 1's adapter and storage.

The legacy `archive` branch once carried a Claude Code tentacle on the now-deleted `nerve`/`octopus` dispatcher architecture. Its **`SSHTransport`** and **SDK-message→event adapter logic** are reused as references; its tentacle wiring is not (rebuilt against today's `AgentTentacle`/`Octomate`/triage model).

---

## Locked decisions

1. **Pattern 1 first**, Pattern 2 as a later follow-up on the same adapter.
2. **Approval model:** bridge Claude's `can_use_tool` / `AskUserQuestion` / `ExitPlanMode` into octomate's deferred-action system (interactive, persisted, cross-channel approval/question cards) — not autonomous-only.
3. **Execution:** both local subprocess **and** remote SSH.
4. **Persistence:** adapt Claude turns into pydantic-ai `ModelMessage`s and reuse the existing `record_agent_run` / `model_messages` stack. **No separate events table.** Add `agent_session_id` on the conversation (for `resume`) and a raw-transcript audit blob on the run (lossless hedge, pre-aligns with Pattern 2).

---

## Architecture in one breath

Triage (inkling, the router) selects the **Claude agent** for a turn → `ClaudeCodeTentacle` runs `ClaudeSDKClient` (local subprocess, or `SSHTransport` to a remote host) → **one adapter** projects the Claude message stream two ways:

- **live** — pydantic-ai-shaped `StreamEvents` so existing channels (Slack/Lark/web) render Claude runs through the unchanged `feelers.timeline.drive()` loop;
- **persisted** — an accumulated `list[ModelMessage]` written through the existing `record_agent_run` path.

Approvals bridge into the deferred-action machinery (same cards as inkling). The Claude `session_id` is stored on the conversation and passed as `resume=` so Claude owns its own coding context; octomate's stored messages are the display/search/triage copy.

### Why adapt to pydantic-ai messages (not store Claude events as-is)
Claude's wire format and pydantic-ai's message model are a near-isomorphism (both are Anthropic-API-shaped turns):

| Claude SDK | pydantic-ai |
|---|---|
| initial prompt | `ModelRequest([UserPromptPart])` |
| `AssistantMessage` → `TextBlock` | `ModelResponse([TextPart])` |
| `AssistantMessage` → `ThinkingBlock` | `ModelResponse([ThinkingPart])` |
| `AssistantMessage` → `ToolUseBlock(id,name,input)` | `ToolCallPart(tool_name, args, tool_call_id)` |
| `UserMessage` → `ToolResultBlock(is_error=False)` | `ModelRequest([ToolReturnPart])` |
| `UserMessage` → `ToolResultBlock(is_error=True)` | `ModelRequest([RetryPromptPart])` |
| `AssistantMessage.usage/model/stop_reason` | `ModelResponse.usage/model_name/finish_reason` |
| `ResultMessage.session_id/cost` | run/conversation metadata (not a message) |

Reuse wins because (a) the live channel render already requires a Claude→pydantic-ai *event* adapter, so the persisted `ModelMessage` list is the same adapter's other projection — one adapter, not two; (b) it sidesteps the `StreamEvents` serialization blocker (the generic `FinalResult[OutputT]` member, noted in `octomate/capabilities/events.py`); (c) web replay reuses the pydantic-ai-native Vercel renderer with no new vocabulary; (d) uniform history/search/triage, no synthetic stubs; (e) `record_agent_run` is reused verbatim. The only honest cost is adapter fidelity (see Risks).

---

## Module layout

**New — `octomate/tentacles/agent/claude/`:**
- `base.py` — `ClaudeCodeTentacle(AgentTentacle[str, None])`
- `adapter.py` — Claude SDK ↔ pydantic-ai messages/events (the shared core; reused by Pattern 2)
- `transport.py` — `SSHTransport` (ported from `archive`)

**Touched:**
- `octomate/config/agents.py` — `ClaudeCodeConfig` / `ClaudeSSHConfig` + a `transport` selector (`local`|`ssh`) + opt-in `claude` field on `AgentsConfig` (centralized config, matching `InklingConfig`; no tentacle-local config module)
- `main.py` — register the Claude tentacle
- `octomate/schemas/triage.py` + `octomate/tentacles/agent/inkling/graph/triage.py` — agent routing
- `octomate/schemas/conversation.py` + `octomate/models/conversation.py` — `agent_session_id`
- `octomate/schemas/runs.py` + `octomate/models/runs.py` — raw transcript blob
- `octomate/managers/conversations.py` — `record_agent_run` extension
- `octomate/base.py` — `kick` live-waiter branch for deferred approvals
- `migrations/` — 2 Alembic revisions (`agent_session_id`, `raw_transcript`)
- `Dockerfile` / `docker-compose.yml` — `openssh-client`, `~/.ssh` mount (SSH path)

---

## Phases

Each phase is independently verifiable. Build order: **0 → 1 → 2 → 3 → 4 → 5 → 6** (Phase 5/SSH is independent and can move earlier if remote testing is needed sooner).

### Phase 0 — Config + tentacle skeleton
- `ClaudeCodeConfig` (`cwd`, `model`, `max_turns`, `description`, `transport: local|ssh`, and a `ClaudeSSHConfig` `ssh` block required when `transport='ssh'`); opt-in `claude` field on `AgentsConfig`.
- `ClaudeCodeTentacle` skeleton: the two abstract methods (`run`, `run_stream_events`) raising `NotImplementedError`, `__aenter__/__aexit__`, an in-memory session map.
- Register in `main.py`.

**Success:** app boots with the `claude` agent registered; existing flows unaffected.

### Phase 1 — Adapter + local streaming run (autonomous)
The core. Build `adapter.py` consuming `ClaudeSDKClient.receive_response()` and emitting **both** projections per the mapping table.
- `run_stream_events()` — open `ClaudeSDKClient(options, transport=None)`, `resume=session_id`, `query(prompt)`, translate each message, yield `StreamEvents`; capture `ResultMessage`; emit a terminal `AgentRunResultEvent` wrapping a **synthesized `AgentRunResult[str]`**.
- `run()` — drain the stream to the terminal result (inkling's pattern).
- Persist via `record_agent_run(...)` with the mapped messages. Run **autonomously** (`permission_mode="acceptEdits"`); approvals arrive in Phase 4.

**Test seam:** bind a dev/web channel's `agent_id` directly to `claude` to exercise reception without triage changes.

**Success:** "claude, do X" streams text + tool calls into the timeline; the run persists as real `ModelMessage`s; conversation history + `search_messages` show it.

**Risk to retire here:** synthesizing `AgentRunResult` (mint a uuid7 `run_id`; `all_messages()` = the mapped list). Confirm pydantic-ai's constructor first.

### Phase 2 — Triage agent routing
- Add `agent_id: str = ""` to `TriageDecision`; agents advertise a `description`.
- Complete the in-flight triage refactor: resolve `agent_for(decision.agent_id or channel.agent_id)` in `RunReception`, and list agents in `TRIAGE_INSTRUCTIONS`. Triage itself stays on inkling.

**Success:** "let claude do X" routes reception to Claude; plain Q&A stays inkling; existing triage behavior/tests intact.

### Phase 3 — Session resume + raw transcript
- `conversations.agent_session_id` (nullable String) + schema field (mutable); scoped by the conversation's existing `agent_tentacle_id`.
- `agent_runs.raw_transcript` (PydanticJSON) + schema field — the lossless audit blob.
- Both via Alembic.
- Extend `record_agent_run(..., agent_session_id=None, raw_transcript=None)` to write messages + session id + transcript in one transaction, then `refresh` (cache stays coherent).
- Tentacle: read `agent_session_id` on `ensure` → `resume=`; write back `ResultMessage.session_id` after the run.

**Success:** a second turn in the same thread resumes the same Claude session (context continuity + session-id continuity); raw transcript stored per run.

### Phase 4 — Approval bridge (deferred-actions)
- `can_use_tool` callback + `PreToolUse` hooks (`AskUserQuestion`, `ExitPlanMode`).
- On a gated tool / question: synthesize a `DeferredToolRequests` → `action_manager.create_batch(...)` → yield `ActionBatchEvent` (channel renders the existing cards) → register `pending_future[batch_id]` and `await` it.
- Add the **`kick` branch**: a `DeferredActionBatchResponse` whose batch has a live Claude waiter resolves the future (+ `resolve_batch`) instead of re-entering `triage_graph`.
- `allow_session` → per-session auto-approve for that tool kind.

**Success:** a Bash/Write call raises an approval card in Slack/web; approve → runs, deny(reason) → fed back to Claude; `AskUserQuestion` renders as a question card; `allow_session` suppresses repeats.

**Documented caveat:** answered against a *live* in-process session → not durable across an octomate restart (unlike inkling deferrals, which are). Acceptable for v1; note it in code.

### Phase 5 — Remote SSH transport
- Port `SSHTransport` from `archive` (interface verified byte-for-byte against the `0.1.80` `Transport` ABC). Spawns `ssh -T host 'cd … && export … && claude … --output-format stream-json --input-format stream-json --permission-prompt-tool stdio'` via `asyncio.create_subprocess_exec`; system `ssh`, no in-process SSH lib.
- Inject when `ssh` is configured; `transport=None` stays the local path.
- `openssh-client` in Dockerfile, `~/.ssh` mount in compose.
- Isolated in `transport.py`; depends on SDK-private `claude_agent_sdk._internal.transport.Transport` (pin to `0.1.80`).

**Success:** with `ssh` set, identical flows run `claude` on the remote host; events/persistence stream back unchanged.

### Phase 6 — Lifecycle polish
- One active run per `ConversationKey`; a new inbound message `interrupt()`s the live client and cleans up.
- Optional thread-continuation seeding (resume the same session for follow-ups in a freshly opened thread).
- **Worktree isolation explicitly deferred.**

**Success:** mid-run message interrupts cleanly; no orphaned clients/transports.

---

## Pattern 2 — mirror native runs → web replay (overview, later)

Decoupled from triage; different mechanism (native app/VS Code = no SDK):
- **Signal:** a global hook in `~/.claude/settings.json` (`UserPromptSubmit` / `PostToolUse` / `Stop`) that `curl -X POST`s `{session_id, transcript_path, cwd}` to an **Octomate FastAPI ingest router** (`octomate.include_router` — web APIs are Octomate routers, not channel tentacles). Hook = "ping + pointer," fire-and-forget.
- **Content:** ingest tails the transcript JSONL (`~/.claude/projects/<slug>/<session_id>.jsonl`, the complete ordered record) through the **same `adapter.py`** (a transcript-shaped parser variant) → `ModelMessage`s + raw blob via `record_agent_run`.
- **Replay:** web read endpoint re-emits stored messages through the existing pydantic-ai-native Vercel renderer.
- **Reuses:** Phase 1 adapter + Phase 3 storage.
- **Deferred decisions:** per-dev-machine hook install (each machine needs the ingest URL + auth token); full-transcript privacy (code/file contents leave the machine); co-located file-read shortcut (if octomate can read `~/.claude/projects` directly, skip the POST).

---

## Cross-cutting risks
- **SDK-private `Transport` coupling** — isolated in `transport.py`, pinned to `0.1.80`; an SDK bump touches one file.
- **Adapter fidelity (highest bug density)** — `tool_use_id ↔ tool_call_id` correlation, tool-result content shapes (image/structured blocks), `is_error → RetryPromptPart`, thinking signatures. Covered by golden-transcript unit tests.
- **Approval durability** — live-session bound (caveat in Phase 4).
- **Synthetic `AgentRunResult`** — fabricated for Claude; confirm pydantic-ai constructor and that triage's use of `result.output` / `result.run_id` / `all_messages()` is satisfied.

## Testing
- **Adapter unit tests** — golden Claude transcript → expected `list[ModelMessage]` + expected `StreamEvents` (covers Phases 1 & 3 and Pattern 2).
- **Triage routing test** — a decision with `agent_id="claude"` lands on the Claude tentacle.
- **Deferred-bridge test** — a fake `DeferredActionBatchResponse` resolves the live future; allow / deny / `allow_session` paths.
- **Manual e2e** — web/dev channel for streaming, approvals, resume.

## Out of scope
Nerve-free architecture untouched; channels and inkling reused unchanged; worktree isolation and the Pattern 2 implementation are follow-ups.

---

## Verified facts (grounding)
- `claude-agent-sdk 0.1.80` is already a dependency (`pyproject.toml`, `uv.lock`) but imported nowhere on `main` — no new dep needed.
- The custom `Transport` ABC (`connect`/`write`/`read_messages`/`close`/`is_ready`/`end_input`) is intact and explicitly blessed for "remote Claude Code connections"; both `query()` and `ClaudeSDKClient(...)` accept `transport=`. The archive's `SSHTransport` ports forward.
- SDK programmatic hook events: `PreToolUse, PostToolUse, PostToolUseFailure, UserPromptSubmit, Stop, SubagentStop, PreCompact, Notification`. `SessionStart`/`SessionEnd` are settings.json shell hooks only (Pattern 2).
- Persistence today: `Conversation → AgentRun → ModelMessage`; messages physically hang off the run (`ModelMessage.run_id`); `Conversation.messages` is a viewonly join; `PydanticJSON` serializes via `to_jsonable_python` with validation owned by the arcanus schema; `record_agent_run` writes a run's messages in one transaction then refreshes the cache.
- All mapped pydantic-ai parts exist (`UserPromptPart`, `ToolReturnPart`, `RetryPromptPart`, `TextPart`, `ThinkingPart`, `ToolCallPart`); the repo's `ModelRequest`/`ModelResponse` transmuters already subclass the pydantic-ai message types.
