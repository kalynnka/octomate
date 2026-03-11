## Plan: Tentacle Channel System for napcat

**TL;DR** — Implement a nanobot-inspired tentacle (channel) architecture for the OneBot 11 / napcat WebSocket protocol. The **Nerve** (central message relay, named after octopus neural architecture) uses **anyio memory object streams** (dual inbound/outbound). A `TentacleManager` dispatches outbound actions and collects inbound events. Events and actions flow directly through the streams — no envelope wrappers; routing metadata (`tentacle_id`) lives on the schema models themselves. The first tentacle connects to napcat via **websockets**. As a prerequisite, rename `adaptor_id` → `tentacle_id` in schemas and fix the broken `__init__.py` imports.

**Steps**

1. **Rename `adaptor_id` → `tentacle_id` in events.py**
   - Affects `OneBotEvent.adaptor_id`, `SendGroupMsgAction.adaptor_id`, `SendPrivateMsgAction.adaptor_id`, `CallApiAction.adaptor_id` (4 occurrences).
   - The `tentacle_id` field on each model is where routing metadata lives — no separate envelope needed.

2. **Fix schemas/\_\_init\_\_.py**
   - Change `from app.schemas.events import ...` → `from octomate.schemas.events import ...`.
   - Remove the `from app.schemas.messages import (...)` block and corresponding `__all__` entries (`Message`, `ModelMessage`, `ModelMessagesTypeAdapter`, `ModelRequest`, `ModelResponse`, `sqlalchemy_materia`) since that module doesn't exist yet.

3. **Add dependencies to pyproject.toml**
   - Add `anyio>=4` (for memory object streams and structured concurrency).
   - Add `websockets>=15` (for the napcat WebSocket tentacle).

4. **Create `octomate/nerve.py` — Nerve (central message relay)**
   - Implement `Nerve` class using two pairs of `anyio.create_memory_object_stream`:
     - `_inbound_send` / `_inbound_receive` — tentacles push `OneBotEventUnion` directly, consumer (agent/processor) reads.
     - `_outbound_send` / `_outbound_receive` — processor pushes action models (`CallApiAction | SendGroupMsgAction | SendPrivateMsgAction`) directly, `TentacleManager` reads and dispatches by `tentacle_id`.
   - Methods: `publish_inbound()`, `consume_inbound()`, `publish_outbound()`, `consume_outbound()`, `close()`.
   - No envelope types — events already carry `tentacle_id`, actions already carry `tentacle_id` and `action` discriminator.
   - Define `ActionUnion` type alias for the outbound action types.

5. **Create `octomate/tentacles/base.py` — Abstract Base Tentacle**
   - Define `BaseTentacle(ABC)` with:
     - `name: str` — unique identifier (stamped as `tentacle_id` on events).
     - `nerve: Nerve` — reference to the shared Nerve.
     - Abstract `async def start()` — connect and begin receiving.
     - Abstract `async def stop()` — graceful disconnect.
     - Abstract `async def send(action: ActionUnion)` — deliver an action to this tentacle's connection.
     - Helper `async def _push_event(event: OneBotEventUnion)` — stamps `tentacle_id = self.name` on the event, then calls `nerve.publish_inbound()`.

6. **Create `octomate/tentacles/napcat.py` — napcat WebSocket Tentacle**
   - Implement `NapcatTentacle(BaseTentacle)` for OneBot 11 WebSocket (forward WS, i.e. octomate connects to napcat).
   - Constructor takes `name`, `ws_url` (e.g. `ws://127.0.0.1:3001`), and `nerve`.
   - `start()`: connect via `websockets.connect()`, spawn a receive loop task using `anyio.create_task_group()`.
   - Receive loop: read JSON frames → parse with `OneBotEventUnion` TypeAdapter → call `_push_event()` (which stamps `tentacle_id`).
   - `send(action)`: serialize the action model to JSON → write to the WebSocket. The `echo` field on `CallApiAction` handles response correlation.
   - `stop()`: cancel the task group, close the WebSocket.
   - Handle reconnection with exponential backoff on disconnect.

7. **Create `octomate/tentacles/manager.py` — TentacleManager**
   - Holds a `dict[str, BaseTentacle]` registry.
   - `register(tentacle)` / `unregister(name)`.
   - `start_all()`: start all tentacles + spawn the outbound dispatcher loop in a task group.
   - Outbound dispatcher loop: `consume_outbound()` from nerve → read `action.tentacle_id` → look up tentacle → call `tentacle.send(action)`.
   - `stop_all()`: stop all tentacles, close the nerve.

8. **Create `octomate/tentacles/__init__.py`** — export `BaseTentacle`, `NapcatTentacle`, `TentacleManager`.

9. **Create `octomate/__init__.py`** if it doesn't exist (empty, for package recognition).

**Proposed directory structure after implementation:**
```
octomate/
├── __init__.py
├── nerve.py                  # Nerve (central message relay) + ActionUnion
├── schemas/
│   ├── __init__.py           # fixed re-exports
│   └── events.py             # adaptor_id → tentacle_id
└── tentacles/
    ├── __init__.py
    ├── base.py               # BaseTentacle ABC
    ├── manager.py            # TentacleManager
    └── napcat.py             # NapcatTentacle (WebSocket)
```

**Verification**
- Run `python -c "from octomate.schemas import OneBotEvent; print(OneBotEvent.model_fields)"` — should show `tentacle_id` instead of `adaptor_id`.
- Run `python -c "from octomate.nerve import Nerve"` — should import cleanly.
- Run `python -c "from octomate.tentacles import NapcatTentacle, TentacleManager"` — should import cleanly.
- Type-check: `uv run pyright octomate/` (or Pylance in-editor) — zero errors.
- Unit test (manual): instantiate `Nerve`, publish/consume events and actions to verify anyio streams work end-to-end.

**Decisions**
- **No envelope wrappers**: `OneBotEvent` already has `tentacle_id`; action models (`SendGroupMsgAction`, `SendPrivateMsgAction`, `CallApiAction`) already have `tentacle_id`. Routing metadata lives on the models themselves — no redundant wrapper layer.
- **Nerve (not "bus")**: named after the octopus's decentralized nervous system — the Nerve connects all tentacles, fitting the octopus theme of octomate.
- **anyio streams over asyncio queues**: anyio `MemoryObjectStream` provides backpressure, type safety, and works with any async backend (asyncio/trio).
- **Dual-queue nerve**: mirrors nanobot — inbound for events, outbound for actions — clean decoupling.
- **websockets library**: mature, well-maintained, naturally async, simple API for the WS tentacle.
- **Forward WS only** (octomate connects to napcat): this is the standard napcat deployment model. Reverse WS (napcat connects to us) can be added as a second tentacle variant later.
