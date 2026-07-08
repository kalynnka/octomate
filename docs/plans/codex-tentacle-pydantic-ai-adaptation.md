# Codex Tentacle With Pydantic-AI Message Adaptation

## Summary

Build `codex` as a first-class `AgentTentacle` that runs Codex through the
`openai-codex` Python SDK, adapts Codex SDK stream events into Pydantic AI stream
events for live rendering, and persists the full Codex run as Pydantic AI
`ModelMessage`s through the existing `ConversationManager.record_agent_run`.

No new persistence tables in v1. Codex replay data lives in `model_messages.parts`
plus `model_messages.metadata`.

The `codex` tentacle should respect Codex's own SDK shape rather than copying the
Claude Code tentacle. The implementation uses the high-level
`openai_codex.AsyncCodex` / `AsyncThread` / `AsyncTurnHandle` API and adapts the
turn-routed `Notification` stream. Claude remains a useful reference for
Octomate's `AgentTentacle` contracts and result persistence only.

## Reconciliation with the current architecture

This plan was updated after reading the code; two items in the original draft were
stale:

- **Routing is not `TriageDecision`.** There is no `TriageDecision` type. Routing
  runs through the reflex graph (`octomate/reflex/graph.py`) via `SummonDecision` +
  `GateCapability` + `ReflexDeps.available_routes`. An agent becomes routable simply
  by being registered (`octomate.connect`) and listed in a channel's
  `ChannelConfig.agents` (`octomate/config/channels.py`), where `agents[0]` is the
  default entry agent and every entry is a summon candidate. The gate reads each
  agent's `description`. So "make Codex routable" = register + list in channel
  config + a good `description`. No triage/reception split is built.

- **Resume uses `external_id`, not metadata scanning.** The Claude tentacle stores
  its resumable session handle on `conversation.external_id` and resumes via the
  SDK's `resume=`. Codex does the same at Octomate's boundary: store the Codex
  thread id in `external_id` and resume via the SDK's `thread_resume(...)`. No
  scanning of prior message metadata for a thread id.

## Codex SDK surface (openai-codex)

Grounded against the installed `openai-codex==0.1.0b3`:

```python
from openai_codex import AsyncCodex, ApprovalMode, CodexConfig, Sandbox

runtime = CodexConfig(config_overrides=(...), cwd=..., env=...)
async with AsyncCodex(config=runtime) as codex:
    thread = await codex.thread_start(
        approval_mode=ApprovalMode.deny_all,
        base_instructions=...,
        cwd=...,
        developer_instructions=...,
        ephemeral=...,
        model=...,
        model_provider=...,
        personality=...,
        sandbox=Sandbox.workspace_write,
    )
    # or resume: thread = await codex.thread_resume(thread_id, ...)
    turn = await thread.turn(
        prompt,
        approval_mode=...,
        cwd=...,
        effort=...,
        model=...,
        output_schema=...,
        personality=...,
        sandbox=...,
        summary=...,
    )
    async for notification in turn.stream():
        ...  # openai_codex.models.Notification
    thread.id  # resumable handle -> conversation.external_id
```

Grounded facts (differ from the original draft):

- The public async facade is `AsyncCodex`, not only the lower-level
  `AsyncCodexClient`. The lower-level client is still useful for understanding
  routing: `AsyncTurnHandle.stream()` yields turn-scoped `Notification`s until
  the matching `turn/completed`.
- SDK launch config is the dataclass `openai_codex.CodexConfig` with
  `codex_bin`, `launch_args_override`, `config_overrides`, `cwd`, `env`, client
  metadata, and `experimental_api`. Pydantic settings can validate this dataclass
  directly, so Octomate keeps `agents.codex.runtime` as the upstream type rather
  than wrapping it.
- Thread settings and turn settings are separate in the SDK. Octomate config
  keeps default SDK call arguments for the shared knobs (`cwd`, `approval_mode`,
  `sandbox`, `model_provider`, `personality`) and thread-only/turn-only knobs
  where useful (`base_instructions`, `developer_instructions`, `ephemeral`,
  `effort`, `summary`, `approval_timeout`). The SDK's raw thread `config`,
  `service_name`, and `service_tier` knobs are intentionally not exposed until a
  concrete use case needs them.
- Resume is `thread_resume(thread_id, ...)`, not `resume_thread`.
- `Sandbox` is a str enum: `read_only`→`"read-only"`,
  `workspace_write`→`"workspace-write"`, `full_access`→`"full-access"`.
- Public `ApprovalMode` only has `auto_review` and `deny_all`. There is no
  `"never"` and no public `"user"` facade. The generated protocol does support
  `approvalPolicy=on-request` + `approvalsReviewer=user`, and the sync
  `CodexClient` exposes an approval-handler callback for those SDK requests.
  Octomate's default `approval_mode` is `user`, which bridges those requests to
  Octomate approval/question cards. `auto_review` stays pure SDK auto-review, and
  `deny_all` never asks.
- `model` is a free-form `str`; there is no model-name enum. `CodexModelName` is
  just the route-label Literal the config/channel `agents` lists select from.
- The event schema lives in `openai_codex.models` as `Notification` subclasses:
  `AgentMessageDeltaNotification`, `ItemStartedNotification`,
  `ItemCompletedNotification`, `ReasoningTextDeltaNotification`,
  `ReasoningSummaryTextDeltaNotification`, `CommandExecutionOutputDeltaNotification`,
  `FileChangeOutputDeltaNotification`, `TurnStartedNotification`,
  `TurnCompletedNotification`, `ThreadTokenUsageUpdatedNotification`,
  `ErrorNotification`, `McpToolCallProgressNotification`, `PlanDeltaNotification`,
  etc. The adapter step pins each one's fields before writing the mapping.

## Step plan (one branch per step, review gate between each)

Branches stack: each is cut off the previous one. After each step is ready, stop
for review before starting the next.

### Step 1 — `feat/codex-config-dep` (dependency + SDK-shaped config) — done

- `pyproject.toml`: add `openai-codex>=0.1` to `dependencies`; `uv lock`.
- `octomate/config/agents.py`:
  - Add `CodexModelName` literal (`"gpt-5.5"`, `"gpt-5.5-pro"`,
    `"gpt-5.3-codex"`, `"gpt-5.1-codex-mini"`) and fold into
    `AgentRouteModelName = KnownModelName | ClaudeCodeModelName | CodexModelName`.
  - Add project `CodexConfig(BaseModel)`: `enabled`, `cwd`,
    `runtime: openai_codex.CodexConfig`, `models: set[CodexModelName]`
    (min_length 1), `approval_mode`, `sandbox`, `approval_timeout`, and the
    default SDK thread/turn arguments Octomate should persist:
    `base_instructions`, `developer_instructions`, `ephemeral`, `model_provider`,
    `personality`, `effort`, `summary`.
  - Add `codex: CodexConfig | None = None` to `AgentsConfig`.
- `octomate.default.yaml`: add a commented `codex:` example under `agents:`.
- Verify: `uv run pytest tests/test_config.py`.

### Step 2 — `feat/codex-adapter` (notification ↔ message/stream translation) — done

- New: `octomate/tentacles/agent/codex/__init__.py`,
  `octomate/tentacles/agent/codex/adapter.py`. The v1 slice is implemented for
  prompt begin, text deltas/completion, reasoning deltas/completion, command
  execution start/output/completion, usage, turn completion, and structured-result
  validation from JSON final text.
- `CodexRunAccumulator` owns the two projections:
  - live `PartStartEvent` / `PartDeltaEvent` / `PartEndEvent` and
    function-tool call/result events for channels;
  - persisted `ModelRequest` / `ModelResponse` messages for
    `ConversationManager.record_agent_run`.
- `begin(user_prompt)` appends the prompt `ModelRequest` with Codex metadata.
- `consume(Notification)` handles the current SDK payloads by exact class and field
  names:
  - `AgentMessageDeltaNotification.delta` accumulates one `TextPart` per
    `item_id`; the final response comes from the final-answer
    `AgentMessageThreadItem` in `ItemCompletedNotification`, falling back to the
    accumulated text.
  - `ReasoningTextDeltaNotification.delta` and
    `ReasoningSummaryTextDeltaNotification.delta` accumulate `ThinkingPart`s.
  - `ItemStartedNotification.item` for `commandExecution` starts a native
    command tool projection.
  - `CommandExecutionOutputDeltaNotification`,
    appends output to the command state.
  - `ItemCompletedNotification.item` closes text/thinking parts or emits a
    command `ToolReturnPart`.
  - `ThreadTokenUsageUpdatedNotification.token_usage.last` maps to
    `RequestUsage`; `RunUsage.requests` increments once per completed turn.
  - `TurnCompletedNotification.turn` sets terminal status/error metadata.
- Every adapted message gets `metadata={"source": "codex", "events": [...]}` with
  compact event records (`method`, `thread_id`, `turn_id`, `item_id`, and dumped
  payload). Keep full raw SDK payloads out of top-level Octomate schema fields.
- Verify: `tests/agent/test_codex_adapter.py` covers prompt/text/reasoning,
  command execution, token usage, turn completion, failed turn metadata, and the
  SDK-native file-change, MCP, dynamic-tool, web-search, plan, error, and
  config-warning event paths.

### Step 3 — `feat/codex-tentacle` (the tentacle) — done

- New: `octomate/tentacles/agent/codex/base.py` (+ export in `__init__.py`).
- `CodexTentacle(AgentTentacle[str, None])`, `in_process = True`, coding/repo
  `description`. `_iter_events` composes any per-run runtime overrides with the
  upstream `openai_codex.CodexConfig` dataclass, then drives `AsyncCodex(config=...)`:
  `thread_resume(conversation.external_id, ...)` when set, else
  `thread_start(...)`, with literals mapped to SDK enums at the boundary
  (`ApprovalMode`, `Sandbox`, `ReasoningEffort`, `ReasoningSummary`,
  `Personality`).
- Start a turn via `await thread.turn(prompt, ...)`; consume
  `async for notification in turn.stream()` through `CodexRunAccumulator`; then
  `record_agent_run(..., external_id=thread.id)`.
- `run` / `run_stream_events` mirror Claude's overload signatures.
- `output_type` is supported by passing `output_schema` to the SDK and validating
  the final JSON text with a `TypeAdapter`. `DeferredToolRequests` output is
  rejected; other pydantic-ai run extras that do not map to Codex are accepted for
  signature compatibility and ignored, matching Claude's current boundary.
- Live-turn interrupt map is keyed by Octomate thread id; a new turn interrupts
  the prior active Codex turn for that Octomate thread.
- Verify: `tests/agent/test_codex_tentacle.py` with a fake SDK client — new-thread
  vs resume-from-`external_id`, records messages, yields stream events + terminal
  `AgentRunResultEvent`.

### Step 4 — `feat/codex-register` (wire it in) — done

- `main.py`: register when enabled —
  `if (codex_config := config.agents.codex) is not None and codex_config.enabled:
  octomate.connect(CodexTentacle("codex", octomate, config=codex_config))`.
- Add `codex` to config route validation and the channel `agents` example lists in
  the yaml so the gate surfaces it as a summon candidate.
- Verify: `uv run pytest tests/test_config.py tests/agent/test_codex_adapter.py
  tests/agent/test_codex_tentacle.py tests/agent/test_claude_tentacle.py
  tests/agent/test_claude_approval.py tests/agent/test_dispatch.py
  tests/channels/web/vercel/test_web_app.py` green.

### Step 5 — `feat/codex-approval-bridge` (human approval/question bridge) — done

- Preserve the SDK modes:
  - `auto_review` maps to public `ApprovalMode.auto_review` and is reviewed by
    Codex's SDK auto-review path.
  - `deny_all` maps to public `ApprovalMode.deny_all` and never asks.
  - `user` uses generated thread start/resume params
    (`approvalPolicy=on-request`, `approvalsReviewer=user`) and a
    `CodexClient` approval handler so live SDK requests become Octomate deferred
    cards.
- Add a small bridged `AsyncCodex` construction path because the public async SDK
  facade does not currently expose the sync client's `approval_handler` argument.
- Command-execution and file-change approval requests present approval cards and
  return SDK decisions (`accept` / `deny`). `allow_session` persists the allowed
  Codex tool name on the conversation and suppresses repeat cards in the live
  session.
- Question/elicitation-shaped SDK requests present question cards and return the
  accepted answer payload to the SDK callback. This is intentionally
  protocol-shaped and can tighten if/when the SDK exposes a typed async question
  hook.
- Verify: focused `tests/agent/test_codex_tentacle.py` coverage for human
  approvals and questions, plus config tests for the new default.

### Step 6 — `feat/codex-parity-gap-pass` (Claude parity gaps) — done

- Broaden native event adaptation so Codex-owned work is persisted as already-run
  native parts: file changes, MCP tool calls, dynamic tools, web search, plan
  updates, error notifications, and config warnings.
- Attach terminal turn status, failed-turn errors, usage, and finish reason to
  persisted Codex responses. A failed turn with no prior response now records a
  synthetic error response before the tentacle raises.
- Extend the human bridge beyond the initial happy path: generic SDK approval
  request names, deny and timeout responses, `allow_session` persistence, and
  conversation permission modes. `accept_edits` auto-approves file changes,
  `bypass_permissions` auto-approves any approval request, while `auto_review`
  and `deny_all` remain pure SDK modes.

## Test Plan

- Config (Step 1): default model set, stale model rejection, SDK runtime
  dataclass parsing, and SDK-named thread/turn settings.
- Codex notification adaptation (Step 2): prompt begin, text delta/completed,
  reasoning, command execution start/output/completion, file changes, MCP,
  dynamic tools, web search, plan deltas, token usage, errors, config warnings,
  and turn completion — each produces valid Pydantic AI messages with Codex
  metadata.
- `CodexTentacle` with a fake SDK client (Step 3): starts a new thread when no
  `external_id` exists; resumes the thread id from `external_id`; records adapted
  messages through `ConversationManager`; yields live Pydantic AI stream events and
  a terminal `AgentRunResultEvent`.
- Routing (Step 4): `available_routes` lists `codex`; the gate can summon it; no
  regression in existing dispatch tests.
- Approval bridge (Step 5): `approval_mode=user` presents Codex SDK approval and
  question requests as live Octomate deferred cards; `auto_review` and `deny_all`
  continue using the public SDK modes without Octomate cards.

## Assumptions

- v1 stores Codex run telemetry as adapted `ModelMessage`s, not a new event table.
- `approval_mode=user` is Octomate's human bridge over the SDK's generated
  `approvalsReviewer=user` protocol. `auto_review` and `deny_all` are delegated to
  the public SDK `ApprovalMode` unchanged.
- Codex resume uses `conversation.external_id` (the Codex thread id), consistent
  with the Claude tentacle.
- Codex routing reuses the existing `SummonDecision` / `GateCapability` mechanism;
  no `TriageDecision` is introduced.
- Web replay reads Codex runs by filtering `metadata.source == "codex"`.

## Open items to confirm

- Whether `openai-codex>=0.1.0b3` is stable enough or should be pinned exactly
  while the SDK API is still beta.
