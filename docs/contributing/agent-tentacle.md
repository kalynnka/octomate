# Adding an agent tentacle

An agent tentacle wraps a runtime so Octomate can drive it: run a turn for a
conversation, stream what happens in Octomate's event vocabulary, raise approvals
and questions as deferred requests, and advertise its models. Inkling is the
smallest complete example; Claude Code is the one to read for a subprocess harness.

Wrap each agent's third-party SDK in its `ink.py` and expose that wrapper as the
tentacle's `ink`. The wrapper follows its SDK's operations and resource lifetime;
there is no shared agent Ink base class or required method set. Codex owns one
shared client, while Claude opens a client for each inspection or turn. Managers,
approval decisions and persistence stay in the tentacle; operation callbacks pass
through method arguments. Claude and Codex use this structure. `DeepseekInk` owns
the shared Remote client's context, multiplexed event routing, session queues and
subscription cleanup. Startup receives callbacks for interaction decisions and
catalog invalidation; Ink sends the resulting replies over Remote. The tentacle
drains active runs before exiting Ink and retains approvals and persistence.
Ink also validates native command catalogs and executes command lines without
attachments. Its execution result preserves native success
and error variants, lifecycle IDs and source-event references; unmatched lines
return `None` and Remote failures raise. The tentacle enters and exits Ink; other
existing Remote operations still use `ink.client`. Inkling keeps its SDK ownership
inline pending migration.

Pass managers explicitly when constructing Claude and Codex tentacles. Their
shared discovery and project lookup use the injected command, project and thread
managers, and their hook ingest receives the same conversation, project and
thread managers. The retained Octomate reference supplies the channel registry
and deployment configuration.

`ClaudeInk` owns initialization reads, SDK message streaming, interruption and the
weak map of active clients keyed by conversation ID. The tentacle supplies SDK
options and the per-turn interruption callback, translates native messages, and
records the result. Closing a stream closes its SDK context before the workspace
is released.

Codex keeps its SDK operations in `CodexInk`: client startup and shutdown,
notifications, discovery queries, MCP configuration, and native thread and turn
operations. `CodexTentacle` loads conversations and saves their native thread IDs
through `ConversationManager`, and passes a catalog invalidation callback when
starting the ink. The ink receives native thread IDs and credential values for
each operation. Applied conversation and credential bindings stay in ink memory
until the native thread closes or the client disconnects. The tentacle also owns
credentials, human approvals and run recording.

## The contract

`AgentTentacle` is abstract over two methods, each taking the same arguments:

```python
async def run(self, user_prompt=None, *, conversation_address, thread_id=None,
              deferred_tool_results=None, deferred_suspender=None, model=None,
              effort=None, conversation_id=None, interactive=True,
              instructions=None, capabilities=None, ...) -> AgentRunResult[...]

def run_stream_events(self, ...same...) -> ReactEventStream[...]
```

A run either has a `user_prompt` or `deferred_tool_results`, never both: the
second is a resumed turn whose answers to a batch become tool results. `effort` is
Octomate's vocabulary, which you map onto the runtime's knob. `interactive` is
false for a commissioned run that has no user to ask. `capabilities` carries the
gateway capability when the run should offer the routing spells.

The stream yields `StreamEvents` and ends with an `AgentRunResultEvent`: Pydantic
AI's own part and tool events, plus Octomate's result segments, todo events,
message-sent events, OAuth events and action batches. The result's output is text,
segments, or `DeferredToolRequests` when the run stopped to ask.

The base class provides claims and routes, model discovery hooks, session
counters, the project lookup for the run's workspace, `resumed_prompt` for a
runtime that takes no tool result back, and `subagent_run`.

## Runtime commands

The optional `probe_commands` and `execute_command` hooks use the
[command schemas](../api/schemas/commands.md). Their defaults report unsupported
without starting a turn. Each agent exposes `discover_commands` for cached discovery,
refresh and command-name completion, backed by the host's command manager and an HTTP
endpoint. The host also provides guarded execution through its command manager and
HTTP API. Codex implements skill discovery and execution; Claude implements
command discovery and execution. DSH implements live session-scoped discovery,
direct results and immediate command-started runs. The remaining runtime adapters and channel
command controls are not wired yet.

Channels can dispatch explicit intent with
`await octomate.kick(CommandSignal(context, invocation, delivery_id))`. Reflex's
command entry delivers direct feedback or consumes the same native agent stream
with gateway capabilities, approvals and normal channel delivery. The context
must already identify the authenticated user and originating surface. The command
manager revalidates it before dispatch; retries use the same delivery ID.
For Trunkline addresses, the browser execution endpoint uses this graph entry and
the existing request-local output sink. Reflex presents command feedback through
channel events; the endpoint emits the returned outcome after graph cleanup.
HTTP execution for IM addresses still calls the manager directly; that integration
remains separate.

Both hooks receive a `CommandContext` resolved by the caller: selected agent, authenticated user,
originating channel address, effective workspace, conversation, model and approval
posture. A new composer has `conversation=None` and `cwd=None`. An existing
conversation's workspace path is resolved without creating its directory.
Discovery must not send a
model prompt or create a visible turn. If the runtime requires a session, return
an unavailable catalog explaining the prerequisite instead of creating one
silently.

The host owns a [command manager](../api/managers/commands.md) at
`octomate.commands`. Its `discover(agent, context, refresh=False)` method caches
one `CommandCatalog` per `CommandCatalogKey(user_id, agent_id, conversation_id)`.
Before a conversation exists, its ID is `None`, so repeated discovery for the same
user and agent shares a cached catalog while the context still matches. The catalog retains its
discovery context; context changes replace that conversation's catalog.
The host injects its tentacle registry and user, conversation, thread, workspace
and gateway managers into the command manager; execution uses these dependencies directly.
Context construction belongs to the caller: the HTTP router's `command_context`
dependency builds it for browser requests, and IM callers build it from their
observed surface. The command manager validates the supplied context without
constructing a replacement.
The command router checks channel enablement and builds context before each lookup, including
cache hits, then calls the resolved agent's
`discover_commands(context, refresh=False, prefix="")` method.
Discovery requires a signed-in user and an agent enabled on the selected channel.
It does not require a linked channel profile, conversation membership or ownership
of the current route. The authenticated user supplies the context's user ID.
Discovery uses the context resolved at the
start of the request; changes while waiting are picked up on the next lookup.

Browser clients call `POST /api/commands/catalog` with a signed-in session and
`X-Octomate-Request: 1`. Its body takes `agent_id`, `address` and optional
`conversation_id`, `model`, `permission_mode`, `refresh` and `prefix`. Model and
posture select a new composer's settings; existing conversations use the stored
selection and ignore those inputs. The router resolves the actual workspace and external
session; clients cannot override them. The response omits conversation history,
external session IDs and session tool grants. See `/docs` for the generated API.

`POST /api/commands/execute` uses the same authentication and context fields, plus
`command_id`, a stable `delivery_id`, and optional raw `arguments`. It requires an
existing conversation and applies the execution access checks below. The server
resolves the descriptor from a fresh catalog. Browser attachments are not supported
yet; a nonempty `attachments` field is rejected before dispatch.

Execution responses use `text/event-stream`. Each SSE `data` field is a JSON
[CommandStreamEvent][octomate.commands.CommandStreamEvent], identified by `event_kind`.
Each completed delivery ends with one
[CommandOutcomeEvent][octomate.schemas.commands.CommandOutcomeEvent]
with `event_kind="command_outcome"` and the `CommandOutcome`
in its `outcome` field, after runtime cleanup and receipt persistence succeed.
For Trunkline, direct and replayed feedback, refusals and agent activity use the
channel's existing wire events before that terminal event. Clients render those
channel events and use the terminal outcome for completion tracking, without
displaying its content again. IM HTTP execution returns direct and replayed
outcomes in the terminal event only. Stream failures close without a terminal outcome.
A disconnect cancels execution. Trunkline's graph does not shield cleanup from
external cancellation, so SDK shutdown and receipt persistence may be interrupted.
Retrying the same delivery returns its saved outcome when present; Trunkline also
presents any saved feedback through the channel. Agent activity is not replayed.
Authentication and request-validation failures remain non-2xx JSON responses.
The OpenAPI document uses version 3.2 and describes
each SSE frame through `itemSchema`, with its JSON data contract in `contentSchema`.
Trunkline execution supplies gateway capabilities and a deferred-action presenter
through Reflex; IM HTTP execution does not yet supply them.

`prefix` filters command names case-insensitively without changing the cached
catalog. A nonmatching prefix returns an empty ready catalog when discovery
succeeded. Argument hints remain upstream text, not argument-value completions.
`refresh=true` bypasses a completed cache entry. A composer cannot discover
workspace-specific commands until its thread exists; adapters needing a workspace
or live session must return unavailable and explain the prerequisite.

The LRU cache retains at most 256 catalogs without time-based expiration. Catalogs
are rediscovered after eviction, explicit refresh, context changes or invalidation.
Active discovery is represented by a loading catalog, with its asyncio task
tracked separately under the same `CommandCatalogKey`. Superseded probes are
canceled and drained before a replacement starts. Each probe has a 10-second timeout.
Concurrent requests for the same conversation share a probe;
explicit refresh bypasses a completed result. Loading and failed results are
reprobed on the next lookup. Returned catalogs preserve runtime descriptor
subclasses and are copied so callers cannot alter the cache.

Adapters must call `octomate.commands.invalidate(agent_id=self.id)` on reconnect
or catalog-change events, optionally narrowing by `conversation_id`. Invalidated
in-flight results return loading so the caller can resolve context again. The host
cancels pending discovery after channels stop and before agents close.

Implement `probe_commands` from the running backend's registry. Return a flat
`CommandCatalog` containing `context`, `descriptors` and `status`. The status is
ready, loading, unsupported, unavailable or failed. Only ready catalogs may
contain descriptors; a successful discovery with none is an empty ready catalog.
Unsupported, unavailable and failed catalogs require a `message` explaining why.
The `limitations` list explains gaps such as skills without a native command catalog.
The owning agent is `context.agent_id`, including for a new composer.

```python
return CommandCatalog(
    context=context,
    descriptors=descriptors,
    status="ready",
)
```

Channels use only these `CommandDescriptor` fields:

| Field | Channel use |
|---|---|
| `id` | Submit the opaque selection to the owning tentacle. IDs must be unique within the scoped catalog; names may repeat. |
| `name` | Show and filter the upstream command or skill name. |
| `description` | Show the upstream description. |
| `argument_hint` | Show a free-form input hint when supplied; never parse it as an argument schema. |
| `accepts_attachments` | Offer attachments only for `true`. `false` means unsupported; `None` means unspecified. |

Extend `CommandDescriptor` with typed attributes in the owning tentacle's schema
module. Descriptors are frozen and hashable. Extension fields must also be hashable:
use tuples for sequences, frozensets for sets, and frozen models for structured
values. For example, a Codex adapter can retain the exact skill path:

```python
class CodexCommandDescriptor(CommandDescriptor, frozen=True):
    path: Path = Field(description="The upstream skill path used for invocation.")
```

Claude aliases or a DSH definition ID belong on their respective descriptor
subclasses in the same way. Map the runtime's invocation identity to the common
`id`; channels must not construct a runtime path or slash line from the display
name. Keep all standard fields' meanings unchanged when extending the model.

Construct catalogs with these concrete descriptor instances. The `descriptors` field
is a set: identical descriptors deduplicate, conflicting definitions with the same
ID are rejected, and upstream order is not preserved. It serializes as a JSON array
and uses Pydantic's `SerializeAsAny` to preserve declared subclass attributes when
serializing the catalog. A channel may parse the result using the base contract
and ignore the additional fields. A tentacle that rehydrates serialized metadata
must validate it with its own concrete model: base-model parsing intentionally
does not reconstruct runtime subclasses. Invocation requests carry only the
selected ID, raw arguments and resolved attachments, not a client-supplied copy
of the runtime descriptor.

`execute_command` receives explicit intent and raw arguments. It is a lazy async
generator: a direct command yields one `CommandResult` with existing message
segments or a `CommandError`; an entry that runs the agent yields normal agent
events ending with `AgentRunResultEvent`. Closing the generator must release its
SDK resources, including when the consumer abandons it. A command starts only
one native invocation, even when its behavior becomes known after execution has
begun. Direct output never needs a synthetic agent result or model history.
For an agent run, forward the supplied suspender and capabilities through the
adapter's normal approval and streaming implementation. Keep direct controls out
of `run` and preserve raw
arguments without injecting chat context. The default never interprets slash text
or forwards unsupported commands to the model.

There is no fixed execution-kind field on a descriptor. Decide behavior for each
invocation: DSH's `/plan off` changes state, while `/plan <message>` also submits
agent input. If a command produces both control feedback and agent activity, its
adapter must expose both through the existing stream events.

Goal commands are excluded by the command manager for every runtime until
automatic continuation has an owned lifecycle. DSH uses the native command ID to
match `command/run` and `command/done`, retaining its subscription until any
immediate native turn ends. Direct feedback accompanying a run is a
`MessageSentEvent`; its final `AgentRunResultEvent` carries the actual run result.
Neither a persistent session watcher nor a separate graph entry is needed for
these commands.

Callers enter `commands.validate(agent, context, invocation, delivery_id=..., ...)`
before execution. They supply context identifying the
authenticated user and a stable, nonempty delivery ID. Execution requires
that user's linked profile and access to the addressed chat surface. The selected
agent must still be enabled on the channel and own the conversation's route;
subagent conversations are not execution targets. These checks run once after
discovery, before dispatch or replay. A changed workspace, external session, model
or approval posture returns `stale`; missing access returns `unavailable`.

Execution requires an existing conversation, refreshes the runtime catalog, validates
command membership and declared attachment support, and holds the conversation's turn guard through stream
cleanup. Normal chat turns use the same guard even when gateway spells are disabled.
Busy conversations are refused immediately; execution never queues or retries.

Validation yields either a refusal/replay outcome or
`(surface, profile, descriptor)`. For the tuple, prepare user capabilities if needed, then
enter `commands.execute(agent, context, invocation, validated, delivery_id=..., ...)`
before leaving validation. The same turn guard spans both calls, and execution
does not repeat validation. User setup errors propagate and release the guard;
no receipt exists until execution starts, so that delivery can be retried.

Before runtime dispatch, the manager commits a command receipt on the addressed
chat surface. Its delivery ID must be unique among inbound messages on that surface;
use the same ID when retrying the same request. After checking current access and
context, matching deliveries return their recorded outcome even if the command is
no longer in the catalog. An ID belonging to another sender, conversation, agent
or invocation is refused. A receipt without an outcome is also refused: its runtime
effects may already have occurred. Pre-dispatch refusals do not create receipts.

The execution context manager yields a direct outcome or an async event generator.
Consume events inside the context so closing it also closes the stream. Pass the
run's gateway session to validation, and its suspender and capabilities to execution
when available. Direct adapter errors
become failed outcomes; cancellation and stream errors propagate to the caller.
Direct outcomes are saved before being yielded. Streams record completion only
after full consumption and cleanup; interruption or failure records a failed outcome
if cleanup completes.
Repeated streamed deliveries return that status, without replaying events. Stream
consumers still own event presentation and native run history. A failed receipt write
prevents dispatch; a failed outcome write leaves the receipt to prevent re-execution.
Attempted invocations invalidate that conversation's catalogs after cleanup, including
failures whose side effects may already have occurred. A native transcript handle
alone does not authorize control of an external CLI session.

## A skeleton

```python
@dataclass
class MyAgentTentacle(AgentTentacle[str, None]):
    """Drives My Runtime, one process per conversation."""

    brand_color: ClassVar[Style | None] = Style(color="#abcdef", bold=True)
    native_id: ClassVar[str | None] = None      # set when it serves a hook router
    in_process: ClassVar[bool] = False          # true if a live run parks on a human answer
    description: str = "What this agent is for, shown to summoning agents."

    def __init__(self, id: str, octomate: Octomate, *, config: MyAgentConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.gateway = config.gateway
        self.claims = dict(config.claims)
        self.permission_modes = (PermissionMode(value="ask", name="Ask"), ...)

    async def discover_models(self) -> None:
        models, claims = await self.probe_catalog()   # keyed provider:model
        self.set_model_catalog(models, claims)

    async def __aenter__(self) -> Self:
        await self.discover_models()
        return await super().__aenter__()        # builds the routes

    async def run(self, user_prompt=None, *, conversation_address, **kwargs):
        result = None
        async with self.run_stream_events(user_prompt, conversation_address=conversation_address, **kwargs) as events:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
        if result is None:
            raise RuntimeError("run completed without a result")
        return result

    def run_stream_events(self, user_prompt=None, *, conversation_address, **kwargs):
        return ReactEventStream(self.iter_events(user_prompt, conversation_address=conversation_address, **kwargs))
```

`iter_events` is where the runtime is driven. The pattern the harness tentacles
share:

1. Resolve the conversation and its resumable handle through the conversation
   manager; a harness session id is stored as the conversation's `external_id`.
2. Ask `run_project` for the thread's project and open its
   [workspace](../concepts/workspaces.md); run inside `async with workspace:` so
   the tree is there for the process and released after it.
3. Launch or reuse the runtime with its local customisation off, so the run speaks
   for the asking person only. Mount Octomate's MCP server if the runtime takes
   one; Claude does it in process, Codex over HTTP with a temporary token.
4. Translate the runtime's events into `StreamEvents` as they arrive, and persist
   the run's messages through `record_agent_run` when it ends.
5. Pause on approvals and questions through the run's suspender, and put the
   batch it hands back on the run's own stream. Wait up to `approval_timeout` and
   answer the runtime; mark the batch expired on timeout so the run can finish.
6. If the gateway recorded a teleport mid-run, interrupt the runtime and end the
   turn through the suspender with the teleport's deferral.

## Native ingest

A runtime people also run by hand gets a hook router and a transcript stream.
Declare `native_id`, return the router from `routers()`, guard it with
`hook_guard` for the `hooks` scope and `hook_sender` to attribute rows, and write a
tailer that assembles turns from the streamed lines, committing each as an
`ExternalAgentRun` with its byte range so a reconnect resumes where it left off.
Add the matching installer under `cli/octomate_cli/tentacles/<runtime>/`, which
must stay importable without the server package. The three existing tailers show
three different transcript shapes.

To support native-history teleport, implement `read_fork_transcript`, which
answers the owner's uploaded history up to its latest whole turn, and
`fork_transcript`, which imports it into the landed thread's empty conversation as
a session the runtime resumes. The base class's `fork` and `validate_fork` do the
rest: ownership, the source thread, its project and the landed thread. Validation
creates no destination or runtime session; `Awake` calls it before teleport opens
the destination, and the import must still validate the history it consumes.

## Register it

Add the config model to `TentacleConfigVariant` in `octomate/config/tentacles.py`,
inheriting `AgentConfig` so `enabled` and `gateway` behave, and a case to the
`match` in `octomate/app.py`. The loader enforces one block per agent type.

## Tests

`tests/support/agents.py` has a scripted agent at the tentacle level and at the
model level; the reflex graph tests drive both. A harness tentacle's own tests
typically fake the SDK client and assert the launch options, the disabled
customisation, and the approval bridge, as `tests/agent/test_claude_tentacle.py`
and `test_codex_tentacle.py` do. Document the agent under `docs/usage/agents/`.
