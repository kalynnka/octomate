# Plan: channel-thread ownership and split histories

> Status: proposed
> Owner: @luhui
> Created: 2026-06-24
> Supersedes the useful parts of
> [cancelled/conversation-transcript.md](cancelled/conversation-transcript.md)
> without reusing its overloaded "transcript" wording.

## AGENTS principles applied here

The design below follows the repository rules in `AGENTS.md`:

- Reason from first principles before changing code, state assumptions, and ask
  when a requested shape conflicts with the repo.
- Keep the change surgical. Do not add wrappers, layers, or configuration unless
  they solve the concrete ownership/history problem.
- Preserve the current Octomate architecture: channels translate and call
  `Octomate`, agents run through concrete tentacles, and persistence goes through
  Arcanus schema/transmuter APIs.
- Model real domain concepts directly instead of hiding variant state in loose
  metadata dictionaries.
- Keep typed boundaries precise: use concrete models, typed variants, and
  `TypedDict` only for simple tool payloads.
- Use the existing `Conversation -> AgentRun -> ModelMessage` stack for model
  memory. Do not turn the chat log into a second model memory.
- Verify every step with focused tests before moving on.

## Success criteria

1. A channel thread that was handed from Inkling to Claude remembers Claude as
   the active owner, so the next user message in that same channel thread routes
   to Claude instead of the channel default.
2. The user-facing channel history is durable even when the agent was not awake
   for every message.
3. A wake prompt is built from all unprompted channel messages that should be
   shown to the active agent, not only the single trigger message.
4. Chat history search searches the channel history by default.
5. A separate model history tool searches the model-message ledger for the
   current agent conversation.
6. Channel messages and the model messages they caused are strongly bound by
   database rows, not by best-effort text matching.

## Names

Use these terms everywhere, including code comments and tool instructions:

- **Channel thread**: the IM surface where people and bots talk. This is keyed
  by channel, chat, and thread. It is not owned by one user and not owned by one
  agent.
- **Chat ledger**: the append-only user-facing messages inside a channel thread.
  This is what humans, other bots, and Octomate agents visibly said.
- **Agent conversation**: the existing per-agent model context. In code this is
  still the current `Conversation` table for the first implementation.
- **Model ledger**: the existing `AgentRun` and `ModelMessage` rows. It includes
  prompts, assistant responses, tool calls, tool results, retries, and thinking.

Avoid calling both ledgers "conversation". The old `Conversation` name is kept
in code for a low-risk migration, but docs and new APIs should call it an
agent conversation.

## Current shape

Today, `ConversationKey` is `ChannelAddress + agent_id`, and `ChannelAddress`
contains `user_id`.

That was enough when a conversation meant "this user talking to this agent in
this IM address". It breaks down for group chat and handoff:

- A group thread with messages from Alice and Bob can become two conversation
  keys because the sender is part of the key.
- A summon/handoff decision lives only inside the run that made it. The next
  inbound message resolves the default route again.
- `HistoryCapability` searches flattened `ModelMessage.message_text`. That is
  useful for the model ledger, but it is not the channel history and it misses
  messages that did not wake an agent.

## Hierarchy

```text
ChannelThread
  key: channel_tentacle_id + chat_type + chat_id + thread_id
  active owner: latest handoff's to_agent_tentacle_id + to_model
  prompt cursor: last channel message included in a wake prompt

  ChannelMessage[]
    one durable user-facing message in the channel thread
    sender/user/bot/agent identity
    typed segments + searchable text

  ChannelHandoff[]
    from agent -> to agent ownership changes for this channel thread

  Conversation[]  # existing agent conversations, one per agent owner
    key: channel_thread_id + agent_tentacle_id
    AgentRun[]
      ModelMessage[]

  message_binding secondary table
    ChannelMessage.model_messages <-> ModelMessage.channel_messages
```

`ChannelAddress` should remain the delivery address because outbound reply
targeting still needs `user_id`. Add a smaller `ChannelThreadKey` for stable
thread identity:

```python
@dataclass(frozen=True)
class ChannelThreadKey:
    channel_tentacle_id: str
    chat_type: ChatType
    chat_id: str
    thread_id: str = ""
```

`ChannelThreadKey.from_address(address)` strips the sender user from the key.
That single distinction is the heart of the fix.

## Data model

### `ChannelThread`

New table and schema, owned by a `ChannelThreadManager`.

Fields:

- `id: uuid.UUID`
- `channel_tentacle_id: str`
- `chat_type: ChatType`
- `chat_id: str`
- `thread_id: str`
- `prompt_cursor_message_id: uuid.UUID | None`
- `status: Literal["active", "closed"]`
- `created_at: datetime`
- `updated_at: datetime`

Unique key:

- `(channel_tentacle_id, chat_type, chat_id, thread_id)`

Relationships and derived attributes:

- `handoffs: RelationCollection[ChannelHandoff]`
- `latest_handoff: ChannelHandoff | None`
- `active_agent_tentacle_id: str | None`
- `active_model: str | None`

`active_agent_tentacle_id` and `active_model` should be hybrid
attributes/properties or association proxies over `latest_handoff`, not mutable
columns. The active owner is still a property of the channel thread, but the
source of truth is the append-only handoff history.

### `ChannelMessage`

New table and schema for the chat ledger.

Fields:

- `id: uuid.UUID`
- `channel_thread_id: uuid.UUID`
- `platform_message_id: str | None`
- `reply_id: str`
- `timestamp: datetime | None`
- `direction: Literal["inbound", "outbound"]`
- `actor_kind: Literal["human", "agent", "bot", "system"]`
- `user_id: str`
- `agent_tentacle_id: str | None`
- `sender: UserProfile`
- `segments: list[MessageSegment]`
- `message_text: str | None`
- `raw: str`
- `created_at: datetime`

Indexes:

- `(channel_thread_id, id)`
- `(channel_thread_id, platform_message_id)`
- `(channel_thread_id, actor_kind)`
- `message_text`

`message_text` is derived from segments for simple `LIKE` search first. Keep the
segments as the source of truth so history rendering does not depend on lossy
text.

### `ChannelHandoff`

New table and schema for ownership changes.

Fields:

- `id: uuid.UUID`
- `channel_thread_id: uuid.UUID`
- `from_agent_tentacle_id: str | None`
- `to_agent_tentacle_id: str`
- `to_model: str | None`
- `reason: str`
- `hint: str`
- `brief: str`
- `source_conversation_id: uuid.UUID | None`
- `target_conversation_id: uuid.UUID | None`
- `source_run_id: str | None`
- `source_model_message_id: uuid.UUID | None`
- `created_at: datetime`

This is the audit log. `ChannelThread.active_agent_tentacle_id` derives from the
latest row and is the routing answer.

### `message_binding`

ORM-backed association table for strong relationships between the two histories.
The message relationships use it as their secondary table.

Fields:

- `channel_message_id: uuid.UUID`
- `model_message_id: uuid.UUID`
- `kind: Literal["prompt_source", "assistant_reply", "assistant_send"]`
- `run_id: str`
- `tool_call_id: str | None`
- `position: int`
- `created_at: datetime`

Relationships:

- `ChannelMessage.model_messages`
- `ModelMessage.channel_messages`

Why this is a secondary table instead of nullable columns:

- One prompt can include many channel messages.
- One channel message can be included in both a triage prompt and a reception
  prompt.
- One assistant model response can produce more than one visible channel
  message, especially with streaming rotation or chunking.

## Wake and prompt pattern

Inbound channel events should be recorded before the mention-only gate.

Flow:

1. `ChannelTentacle.ingest` decodes and enriches a `MessageEvent`.
2. `ChannelThreadManager.record_inbound(event)` ensures the `ChannelThread`,
   appends a `ChannelMessage`, and returns both.
3. If the message does not wake the agent, stop. The chat ledger still has the
   message.
4. If it wakes the agent, `Octomate.kick` receives a signal containing the
   `channel_thread_id` and trigger `channel_message_id`.
5. Dispatch resolves the active owner:
   - If `ChannelThread.active_agent_tentacle_id` resolves from
     `latest_handoff`, use that agent and model.
   - Otherwise use the channel's configured default route.
6. Before the run, fetch pending prompt messages:
   - `channel_thread_id` matches.
   - `id` is greater than `prompt_cursor_message_id`, when present.
   - `id` is less than or equal to the trigger message id.
   - Exclude outbound messages by the active agent.
   - Include humans and other bots/agents.
7. Build the user prompt from those channel messages in chronological order.
8. After the run's user `ModelRequest` is persisted, relate it to every included
   `ChannelMessage` through the secondary binding table with
   `kind="prompt_source"`.
9. Advance `prompt_cursor_message_id` to the latest included channel message.

This keeps model history small and honest. The agent sees the stacked messages
that have not yet been presented as a prompt. Older chat context is available
through tools, not pasted into every run.

## Handoff pattern

Summon currently records a decision for the current run, then the graph dispatches
the next agent. Add durable ownership at the moment dispatch accepts that
decision.

Flow:

1. Agent A calls `summon` for Agent B.
2. Dispatch validates the route as it does today.
3. Dispatch resolves or creates the target channel thread.
4. Dispatch ensures Agent B's agent conversation for that channel thread.
5. Dispatch writes a `ChannelHandoff` row.
6. `ChannelThread.active_agent_tentacle_id` and `active_model` now resolve from
   that latest handoff.
7. Dispatch runs Agent B.
8. The next inbound message in the same channel thread resolves Agent B from
   `ChannelThread`, bypassing the channel default.

Every triage -> reception route is a handoff, even when the route is the
configured/default reception. Writing that handoff is the explicit ownership
claim, and it keeps the next kick in the thread routed to the same agent and
model so the agent conversation cache remains valid. A direct triage answer that
does not route to reception does not claim ownership.

If a later agent summons another agent, write another handoff row. Chained
handoffs are just new rows; the active owner derives from the latest row.

## Outbound recording and reply binding

All visible Octomate output should become `ChannelMessage` rows:

- Final markdown replies.
- Final segment replies.
- Streaming answer rotations.
- Mid-run `send_message` events.
- Deferred action cards are deliberately omitted from the chat ledger. Leave a
  short code comment at the presenter noting that omission, because they are UI
  controls rather than chat messages.

The binding should be created after `ModelMessage` rows exist:

- Final assistant text or segment output relates to the assistant
  `ModelResponse` with `kind="assistant_reply"`.
- Mid-run `send_message` relates to the response/tool call that produced the
  send event with `kind="assistant_send"` and `tool_call_id`.

Implementation note: for streaming and mid-run sends, platform output can happen
before the run is fully persisted. Use a typed pending-output record in memory
for the run, keyed by run id and tool call id or output part, then reconcile to
the secondary binding table immediately after `record_agent_run` refreshes the
conversation. Avoid storing unresolved binding metadata in `ChannelMessage`
itself.

## History tools

The user-facing `history` capability should become chat-ledger first.

Tools:

- `search_history(query, actor_kind=None, limit=10)`: alias for
  `search_chat_history`; searches `ChannelMessage.message_text` in the current
  channel thread.
- `search_chat_history(query, actor_kind=None, limit=10)`: explicit chat-ledger
  name.
- `read_history_before(channel_message_id, limit=10)` and
  `read_history_after(channel_message_id, limit=10)`: page chat-ledger messages.
- `read_related_model_messages(channel_message_id)`: return
  `ChannelMessage.model_messages` for a chat message.

Add a separate model-history capability:

- `search_model_history(query, role=None, limit=10)`: the current
  `search_messages` behavior, scoped to the current agent conversation.
- `read_model_history_before(model_message_id, limit=10)` and
  `read_model_history_after(model_message_id, limit=10)`: the current paging
  behavior.
- `read_related_chat_messages(model_message_id)`: return
  `ModelMessage.channel_messages` for a model message.

Instruction wording:

- Chat history tells you what happened in the channel thread, including messages
  sent while you were asleep.
- Model history tells you what this agent conversation previously prompted,
  reasoned through, sent, and received from tools.
- Prefer chat history when the user refers to what people said. Use model
  history when you need to inspect your prior work around that chat.

## Units of work

### UoW 1: Persistence foundation

Add models, schemas, exports, and an Alembic migration for:

- `channel_threads`
- `channel_messages`
- `channel_handoffs`
- `message_binding`

Add `channel_thread_id` to `conversations` and keep the existing address columns
during the transition.

Success checks:

- `Base.metadata.create_all` includes all tables.
- Arcanus can create, load, mutate, and refresh `ChannelThread`.
- The unique `ChannelThread` key ignores `user_id`.
- Existing conversation tests still pass.

### UoW 2: `ChannelThreadManager`

Add a manager that owns chat-ledger persistence through Arcanus:

- `ensure_thread(address: ChannelAddress | ChannelThreadKey)`
- `record_inbound(event: MessageEvent)`
- `record_outbound(...)`
- `pending_prompt_messages(thread, trigger_message_id, active_agent_id)`
- `advance_prompt_cursor(thread, message_id)`
- `record_handoff(...)`
- `relate_channel_model_messages(...)`
- `search_chat_messages(...)`
- `chat_messages_before(...)`
- `chat_messages_after(...)`

Success checks:

- Manager tests prove group messages from different `user_id`s land in one
  channel thread.
- Pending prompt messages include unprompted human/bot messages and exclude the
  active agent's own outbound messages.
- Prompt cursor advancement is persisted and cache-coherent.

### UoW 3: Inbound recording before wake filtering

Wire `ChannelTentacle.ingest` through the manager:

- Decode/enrich/download as today.
- Record inbound channel message.
- Apply mention-only filtering after recording.
- Pass the trigger channel message id into `UserMessageSignal`.

Success checks:

- Unmentioned group messages are stored and do not wake the agent.
- The next mention in that group thread builds a prompt containing the stored
  unmentioned messages plus the trigger.
- Existing Slack/Lark/Napcat ingest tests keep their wake behavior.

### UoW 4: Handoff ownership routing

Resolve owner before picking the channel default:

- Add `Octomate.channel_threads` as the project-level manager.
- On user wake, resolve `ChannelThread.active_agent_tentacle_id` from
  `latest_handoff`.
- On every triage -> reception route and every summon, write `ChannelHandoff`
  before or while dispatching the target agent.
- Ensure the target agent conversation belongs to the same `channel_thread_id`.

Success checks:

- Regression test: Inkling summons Claude, Claude answers, then the next message
  in the same channel thread routes to Claude.
- Chained summon updates ownership from Agent A to Agent B to Agent C.
- A direct triage answer that does not route to reception does not accidentally
  claim ownership.

### UoW 5: Prompt-source bindings

Bind wake prompt messages to the persisted user `ModelRequest`:

- Build the prompt from `ChannelMessage` rows.
- After `record_agent_run`, identify the new user `ModelRequest`.
- Relate each included channel message to the user `ModelRequest` with a
  `prompt_source` binding row.
- Advance the prompt cursor only after relationship rows are written.

Success checks:

- A wake caused by three stacked channel messages relates all three to the same
  user `ModelRequest`.
- If triage and reception both see the same channel message, the secondary table
  can represent both bindings.
- No binding is created by text matching.

### UoW 6: Outbound chat-ledger recording and assistant bindings

Record visible agent output:

- Non-streaming markdown and segment presentation.
- Streaming timeline answer messages and rotations.
- `send_message` mid-run events.

Then reconcile output records to persisted assistant `ModelResponse`s:

- `assistant_reply` for final replies.
- `assistant_send` for send-tool output, with `tool_call_id`.

Success checks:

- A final assistant reply has a `ChannelMessage` related to its
  `ModelResponse`.
- A mid-run send has a `ChannelMessage` relationship row with the correct
  `tool_call_id`.
- Streaming answer rotation records each visible output message without
  duplicating text.

### UoW 7: Split history capabilities

Change tool behavior:

- Keep `search_history` as a chat-history tool for compatibility.
- Add explicit `search_chat_history`.
- Add model-history tools under separate names.
- Update instructions so agents know which ledger they are searching.

Success checks:

- Existing model-history tests move to `search_model_history`.
- New chat-history tests prove `search_history` finds messages that never woke
  an agent.
- Relationship tools can jump from a chat message to the model messages that
  used it.

### UoW 8: Compatibility cleanup

After the new path is covered:

- Update `ConversationManager.ensure` to resolve by `channel_thread_id +
  agent_tentacle_id` when the thread id is available.
- Keep a compatibility path from `ChannelAddress` for older tests and direct web
  runs while the web/dev channel is moved to the same channel-thread path.
- Make Inkling use the project-level conversation manager in production wiring,
  so all agent conversations are under the same source of truth.
- Move web/dev UI onto the channel pattern instead of preserving a separate
  direct-drive history path.
- Update docs and comments that still imply `Conversation` is a human chat.

Success checks:

- Existing tests for `ConversationManager`, deferred actions, Claude approval,
  triage dispatch, and history pass.
- A fresh process can reload channel thread ownership and continue routing to the
  handoff owner.

## Verification suite

Run at least:

- `uv run pytest tests/agent/test_conversation_manager.py`
- `uv run pytest tests/agent/test_history.py`
- `uv run pytest tests/agent/test_dispatch.py`
- `uv run pytest tests/agent/test_triage_graph.py`
- `uv run pytest tests/channels`

Add focused tests for:

- Group chat multi-user ledger identity.
- Unmentioned message accumulation.
- Handoff ownership survival across cold `Octomate`/manager reload.
- Prompt-source bindings.
- Assistant-reply and assistant-send bindings.
- Chat-history search vs model-history search.

## Non-goals

- No backfill of old conversations.
- No vector search in the first pass. Plain text contains search is enough.
- No rename of the `conversations` table in the first pass.
- No new dispatcher, nerve, or channel lifecycle abstraction.
- No attempt to replay full model activity from the chat ledger. Model activity
  stays in the model ledger.

## Resolved decisions

1. Triage -> reception writes a handoff and declares ownership, even when the
   reception route is the configured default. The next kick in that channel
   thread routes to the same agent and model.
2. Deferred action cards are omitted from the chat ledger. Leave a code comment
   at the presenter so the omission is explicit.
3. Web/dev follows the channel pattern instead of keeping a separate direct-drive
   history path.
