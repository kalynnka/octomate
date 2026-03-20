# Octomate Architecture Redesign

## Current Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │                  Octopus                    │
                         │  ┌──────────┐  ┌────────────┐  ┌────────┐  │
                         │  │  Agent    │  │  Message    │  │ Memory │  │
                         │  │ (single) │  │  Store      │  │ (mem0) │  │
                         │  └────┬─────┘  └────────────┘  └────────┘  │
                         │       │                                     │
                         └───────┼─────────────────────────────────────┘
                                 │
                     ┌───────────┴───────────┐
                     │     OctopusNerve      │
                     │  ┌─────────────────┐  │
                     │  │  MessageBuffer  │  │
                     │  │  inbound stream │  │
                     │  │  outbound stream│  │
                     │  │  tentacle dict  │  │
                     │  └─────────────────┘  │
                     └───┬──────────────┬────┘
                         │              │
              ┌──────────┴──┐    ┌──────┴────────┐
              │ NapcatTent. │    │  LarkTentacle  │
              │  (OneBot)   │    │  (Feishu SDK)  │
              └──────┬──────┘    └──────┬─────────┘
                     │                  │
                QQ (napcat)        Feishu (Lark)
```

Problems:

- Single agent handles all tentacles — can't customize per platform
- Nerve is too thin to justify as a class, but owns responsibilities that should be split
- Event schema (`OneBotEvent`) is named after one platform, yet serves as lingua franca
- Both tentacles duplicate image pipeline, file management, and action dispatch logic
- Agent has no tools to query platform APIs (user profiles, group info, etc.)
- No support for background-triggered messages (cron, webhooks, admin)

## Target Architecture

```
                              Sources
                ┌──────────┬─────┴──────┬──────────┐
                │          │            │          │
           Tentacles    CronJob    Webhook   AdminAPI
                │          │            │          │
                ▼          ▼            ▼          ▼
        ┌──────────────────────────────────────────────┐
        │                    Nerve                      │
        │                                               │
        │  ┌────────────────┐   ┌────────────────────┐  │
        │  │ Tentacle        │   │ Runtime Registry   │  │
        │  │ Registry        │   │                    │  │
        │  │                 │   │ napcat → Runtime A  │  │
        │  │ napcat → tent.  │   │ lark   → Runtime B  │  │
        │  │ lark   → tent.  │   │ cron   → Runtime C  │  │
        │  └────────────────┘   └────────────────────┘  │
        │                                               │
        │  ┌─────────────────────────────────────────┐  │
        │  │ Dispatch Loop                            │  │
        │  │ receive message from any source           │  │
        │  │ → resolve agent runtime by routing rules  │  │
        │  │ → runtime.handle(session, messages)       │  │
        │  │                                           │  │
        │  │ receive action from any runtime            │  │
        │  │ → resolve tentacle by tentacle_id          │  │
        │  │ → tentacle.act(action)                     │  │
        │  └─────────────────────────────────────────┘  │
        └──────────────────────────────────────────────┘
                         │              │
              ┌──────────┴──┐    ┌──────┴────────┐
              │ AgentRuntime │    │ AgentRuntime   │
              │ (napcat)     │    │ (lark)         │
              │              │    │                 │
              │ Agent        │    │ Agent           │
              │ System prompt│    │ System prompt   │
              │ Toolset      │    │ Toolset         │
              │ Msg store    │    │ Msg store       │
              │ Memory       │    │ Memory          │
              └──────────────┘    └────────────────┘
```

### Nerve (evolved)

No longer a thin stream wrapper. Now the central coordination layer:

- **Tentacle registry** — lookup tentacles by name for outbound dispatch and for tools that need platform API access
- **Runtime registry** — maps routing keys (tentacle_id, or finer-grained rules) to AgentRuntime instances
- **Inbound dispatch** — accepts messages from any source (tentacles, cron, webhooks), resolves the target runtime, delivers
- **Outbound dispatch** — accepts actions from any runtime, resolves the target tentacle, delivers
- **Source-agnostic** — background services (cron, webhooks) inject messages the same way tentacles do

### AgentRuntime (new, extracted from Octopus)

Each runtime is an isolated agent context:

- Owns its own pydantic-ai `Agent` instance (model, system prompt, output type)
- Owns its own toolset (skill manager + tentacle API tools)
- Owns its own message store (conversation history per session)
- Owns its own memory (mem0 instance, optional)
- Handles the `think()` loop: build prompt → run agent → deliver responses

Multiple runtimes can share the same tentacle (e.g., two agents both respond on QQ in different groups). One runtime can handle multiple tentacles (e.g., a shared agent for all platforms).

### BaseTentacle (enriched)

```
BaseTentacle
├── config, name, self_id, self_name
├── FILES_ROOT / save_dir computation
├── MessageBuffer (batching before forwarding to nerve)
├── prepare_inbound_message()   ← orchestrates download pipeline
│   └── abstract download_image(seg, save_dir)
├── prepare_outbound_message()  ← orchestrates upload pipeline
│   └── abstract prepare_image(seg)
├── act(action)                 ← base dispatch, extracts SendTarget
│   └── abstract send_message(target, segments)
├── abstract introspect()
├── abstract activate()
├── abstract deactivate()
└── build_toolset() → FunctionToolset
    ├── get_user_profile(user_id)      optional
    ├── get_group_info(group_id)       optional
    ├── list_group_members(group_id)   optional
    └── react_to_message(msg_id, ...)  optional
```

Subclasses only implement platform-specific logic: how to connect, how to download/upload images, how to send messages, and which API tools they support.

### Schema Renames

- `OneBotEvent` → `Event`
- `OneBotEventUnion` → `EventUnion`
- `inbound_adapter` stays in `napcat.py` as a napcat-specific concern
- No structural changes to `MessageEvent`, `GroupMessageEvent`, `PrivateMessageEvent` — they're good as lingua franca

### Agent Output: Structured + Tool Escape Hatch

```
Normal reply flow:
  Agent returns list[AgentMessage]
  → Runtime converts to SendGroupMsg / SendPrivateMsg
  → Nerve dispatches to tentacle

Cross-chat / proactive flow:
  Agent calls send_message(tentacle_id, chat_id, segments) tool
  → Tool pushes action through Nerve directly
  → Agent still returns list[AgentMessage] for originating session (can be empty)
```

The `send_message` tool is provided by the Nerve (not the tentacle), since it needs to resolve any tentacle by name.

## Implementation Plan

### Phase 1: Schema cleanup

Rename OneBot-specific naming to platform-neutral names. No logic changes.

1. Rename `OneBotEvent` → `Event` in `schemas/events.py`
2. Rename `OneBotEventUnion` → `EventUnion`
3. ~~Move `inbound_adapter` (the OneBot JSON deserializer) from `schemas/adaptors.py` into `tentacles/napcat.py`~~ Done — `adaptors.py` removed, content moved to `actions.py`
4. Update all imports across the codebase

### Phase 2: Enrich BaseTentacle

Pull duplicated logic up from both tentacles into the base class.

5. Move `FILES_ROOT` into `BaseTentacle` as a class-level constant
6. Add `save_dir(event)` helper that computes `FILES_ROOT / self.name / subdir`
7. Move `prepare_inbound_message()` into `BaseTentacle` with the shared pattern (find image segments → download concurrently). Add abstract `download_image(seg, save_dir)` for subclasses
8. Move `prepare_outbound_message()` into `BaseTentacle`. Add abstract `prepare_image(seg)` for subclasses
9. Add `SendTarget` dataclass and refactor `act()` to extract target info from any action type, then call abstract `send_message(target, segments)`
10. Move `MessageBuffer` ownership into `BaseTentacle` — each tentacle batches its own inbound messages before forwarding
11. Refactor `NapcatTentacle` to use the new base methods (remove duplicated code)
12. Refactor `LarkTentacle` to use the new base methods (remove duplicated code)

### Phase 3: Tentacle API toolsets

Expose platform APIs as agent tools.

13. Define return models for tentacle APIs: `UserProfile`, `GroupInfo`, `MemberInfo`
14. Add optional capability methods to `BaseTentacle`: `get_user_profile()`, `get_group_info()`, `list_group_members()`, `react_to_message()` — default returns `None` / empty list
15. Add `build_toolset()` to `BaseTentacle` that wraps implemented capabilities as a `FunctionToolset`
16. Implement capability methods in `NapcatTentacle` (wrapping OneBot HTTP API)
17. Implement capability methods in `LarkTentacle` (wrapping Lark SDK)

### Phase 4: AgentRuntime extraction

Split the monolithic Octopus into Nerve + AgentRuntime.

18. Create `AgentRuntime` class, extracting from `Octopus`: agent creation, message store, memory, `think()` loop
19. `AgentRuntime.__init__` takes: `MindConfig`, `SkillManager`, tentacle toolset (from phase 3)
20. `AgentRuntime.handle(session_key, batch)` — the core think loop, moved from `Octopus.think()`
21. Runtime auto-loads the correct tentacle toolset based on the session's tentacle_id

### Phase 5: Evolve the Nerve

Transform OctopusNerve from thin stream wrapper to coordination layer.

22. Add runtime registry to Nerve: `dict[str, AgentRuntime]` with routing rules
23. Add `register_runtime(routing_key, runtime)` method
24. Replace the anyio streams with direct method dispatch: `nerve.deliver(session_key, batch)` resolves runtime and calls `runtime.handle()`
25. Outbound: runtimes call `nerve.dispatch(action)` which resolves tentacle and calls `tentacle.act()`
26. Add `nerve.inject(session_key, messages)` for non-tentacle sources (cron, webhooks, admin) to submit messages into the same pipeline

### Phase 6: Slim down Octopus

Octopus becomes the top-level application bootstrap, not the brain.

27. Move agent/memory/store logic out of Octopus (already in AgentRuntime)
28. Octopus becomes: read config → create nerve → create tentacles → create runtimes → register routing → start nerve
29. `main.py` simplifies to: create `OctomateConfig` → `Octopus(config).run()`

### Phase 7: send_message tool

Add the cross-chat escape hatch for agents.

30. Implement `send_message(tentacle_id, chat_id, segments)` tool on the Nerve
31. Register it as an always-available tool in every AgentRuntime
32. Agent output flow unchanged for normal replies; tool used only for cross-chat/proactive scenarios
