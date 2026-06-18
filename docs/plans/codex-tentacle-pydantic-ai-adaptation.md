# Codex Tentacle With Pydantic-AI Message Adaptation

## Summary

Build `codex` as a first-class `AgentTentacle` that runs Codex through the Python SDK, adapts Codex SDK stream notifications into Pydantic AI stream events for live rendering, and persists the full Codex run as Pydantic AI `ModelMessage`s through the existing `ConversationManager.record_agent_run`.

No new persistence tables in v1. Codex replay data lives in `model_messages.parts` plus `model_messages.metadata`.

## Key Changes

- Add `openai-codex` as a project dependency and add `agents.codex` config with:
  - `enabled: bool = true`
  - `model: str | None = None`
  - `sandbox: "read_only" | "workspace_write" | "full_access" = "workspace_write"`
  - `approval_policy: "never"` for v1 to avoid durable paused Codex turns.
  - `cwd: str | None = None`, defaulting to the current repo/workdir.

- Add `octomate.tentacles.agent.codex.CodexTentacle`.
  - Implement the existing `AgentTentacle` contract.
  - `run()` drains the same stream path and returns `AgentRunResult[str]`.
  - `run_stream_events()` streams adapted Pydantic AI events plus a terminal `AgentRunResultEvent`.
  - Unsupported Pydantic-agent-only kwargs such as custom `output_type`, deferred resume, toolsets, and capabilities fail fast with a clear `ValueError`.

- Adapt Codex events into Pydantic AI messages.
  - Initial user prompt becomes `ModelRequest(parts=[UserPromptPart(...)])`.
  - Completed Codex agent messages become `ModelResponse(parts=[TextPart(...)])`.
  - Reasoning summaries become `ModelResponse(parts=[ThinkingPart(...)])`.
  - Item starts become `ModelResponse(parts=[ToolCallPart(tool_name="codex_<item_type>", args=...)])`.
  - Item completions for non-text work become `ModelRequest(parts=[ToolReturnPart(...)])`.
  - Deltas/lifecycle/control events become synthetic `ToolCallPart` messages with `tool_name="codex_event"`.
  - Every adapted message gets metadata like:
    `{"source": "codex", "method": "...", "sequence": n, "thread_id": "...", "turn_id": "...", "item_id": "...", "payload": ...}`.

- Adapt Codex events into live Pydantic AI stream events.
  - Agent text deltas map to `PartStartEvent` / `PartDeltaEvent(TextPartDelta)` / `PartEndEvent`.
  - Reasoning deltas map to `ThinkingPart` events.
  - Command/file/MCP/web/plan work maps to `FunctionToolCallEvent` and `FunctionToolResultEvent`.
  - Existing channel timelines should render these without new channel-specific renderers.

- Reuse `ConversationManager`.
  - `CodexTentacle` calls `conversation_manager.ensure(...)`.
  - After the Codex turn completes, it calls `record_agent_run(...)` with the adapted `ModelMessage` list.
  - Recover the last Codex SDK thread id by scanning prior conversation messages for Codex metadata; resume that thread when present, otherwise start a new SDK thread.

- Make Codex routable by triage.
  - Extend `TriageDecision` with `agent_id: str = ""`.
  - Update triage instructions to list available agents and choose `agent_id="codex"` for explicit Codex requests or coding/repo execution work.
  - Split triage state into triage agent and reception agent so Inkling can triage while Codex handles the reception run.
  - Default empty `agent_id` preserves current behavior.

- Register Codex in app bootstrap.
  - Build and register `codex` when `config.agents.codex.enabled` is true.
  - Keep channel defaults pointing at `inkling`; triage selects Codex per request.

## Test Plan

- Unit test Codex event adaptation:
  - text delta/completed message
  - reasoning event
  - command execution start/completion
  - file change event
  - turn lifecycle/error event
  - verify all produce valid Pydantic AI messages with Codex metadata.

- Unit test `CodexTentacle` with a fake SDK client:
  - starts a new thread when no prior metadata exists
  - resumes prior thread id from conversation history
  - records adapted messages through `ConversationManager`
  - yields live Pydantic AI stream events and terminal `AgentRunResultEvent`.

- Triage tests:
  - existing routing behavior still passes when `agent_id` is empty.
  - decision with `agent_id="codex"` runs reception on the Codex tentacle.
  - unknown `agent_id` raises the existing unknown-agent error.

- Integration checks:
  - `uv run pytest tests/agent/test_conversation_manager.py tests/agent/test_triage_graph.py tests/agent/test_dispatch.py`
  - add new `tests/agent/test_codex_tentacle.py`.

## Assumptions

- v1 intentionally stores Codex run telemetry as adapted `ModelMessage`s, not a new event table.
- v1 does not implement durable Codex approval pause/resume; approval-related Codex events are persisted/rendered as events, while Codex runs use `approval_policy="never"`.
- Codex replay in web will read from conversation messages by filtering `metadata.source == "codex"`.
- Existing triage WIP should be made green before or during this implementation, especially the current `state.agent_id` path.
