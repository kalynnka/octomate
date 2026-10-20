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
the selected harness's native name, validated against the model's advertised
levels and passed through unchanged. `interactive` is
false for a commissioned run that has no user to ask. `capabilities` carries the
gateway capability when the run should offer the routing spells.

The stream yields `StreamEvents` and ends with an `AgentRunResultEvent`: Pydantic
AI's own part and tool events, plus Octomate's result segments, todo events,
message-sent events, OAuth events and action batches. The result's output is text,
segments, or `DeferredToolRequests` when the run stopped to ask.

The base class provides claims and routes, model discovery hooks, session
counters, the project lookup for the run's workspace, `resumed_prompt` for a
runtime that takes no tool result back, and `subagent_run`.

## Native sandbox verification

Enable the harness's native sandbox and test the boundaries it actually enforces.
A working directory alone does not restrict file access. Test direct file tools
and child processes, including a workspace CLI following instructions from a
skill. A skill grants no additional filesystem authority.

The macOS audit below compares the original driven configuration with the native
settings checked on 16 October 2026. Counts are **passed / failed**, not command
exit codes. A denied outside operation passes; a denied workspace operation fails.
The expectations deliberately test workspace-only access, which is stricter than
these native presets. No container, outer Seatbelt profile or microVM was used.
Inkling's results are retained from the earlier audit; its settings were unchanged.

| Harness | Original shell checks | Current shell checks | Current file-tool checks | What is still lacking |
|---|---:|---:|---:|---|
| Codex | 6 / 10 | 6 / 10 | Covered through native command execution | Native workspace-write allows outside reads, programs, shared temp and `cd`. Full access and approved escalations expand authority. |
| Claude Code | 4 / 12 | 14 / 2 | 4 / 0 | `cd` and shared-temp reads remain available. Native system/runtime and temporary-storage exceptions remain; this does not isolate the controller process. |
| DeepSeek Harness | 6 / 10 | 6 / 10 | 1 / 3 | Outside reads, outside programs, child processes, shared temp and `cd` remain available. The native policy has no read allowlist or shared-temp exclusion setting. |
| Inkling | 4 / 12 | 4 / 12 | 4 / 0 | File-tool checks do not constrain Shell or its descendants. No native shell sandbox was added. |

Each shell column uses the same 16 checks. Outside-file fixtures are sibling
paths under the host user's home, outside both the workspace and system temporary
directories. Shared-temp checks have separate fixtures. These results do not
establish denial of every possible outside path. Codex and DeepSeek allow writes
to native temporary roots even when they are outside the workspace.

| Check and expected result | Codex | Claude | DeepSeek | Inkling |
|---|---|---|---|---|
| Read workspace file: allow | Pass | Pass | Pass | Pass |
| Write workspace file: allow | Pass | Pass | Pass | Pass |
| Read workspace skill: allow | Pass | Pass | Pass | Pass |
| Run skill's workspace CLI: allow | Pass | Pass | Pass | Pass |
| Read outside absolute path: deny | Fail | Pass | Fail | Fail |
| Read outside relative path: deny | Fail | Pass | Fail | Fail |
| Read through outside symlink: deny | Fail | Pass | Fail | Fail |
| Write outside workspace: deny | Pass | Pass | Pass | Fail |
| Write through outside symlink: deny | Pass | Pass | Pass | Fail |
| Execute outside program: deny | Fail | Pass | Fail | Fail |
| Interpret outside script: deny | Fail | Pass | Fail | Fail |
| Change directory outside: deny | Fail | Fail | Fail | Fail |
| Give skill CLI an outside file: deny | Fail | Pass | Fail | Fail |
| Read outside through a child shell: deny | Fail | Pass | Fail | Fail |
| Read shared temp file: deny | Fail | Fail | Fail | Fail |
| Write shared temp file: deny | Fail | Pass | Fail | Fail |

The `cd` check only changes directory and prints a marker; it does not show that
data can be read there. System binaries remain executable. The file-tool checks
separately allow one workspace read and deny outside absolute, traversal and
symlink reads. Claude's original file-tool test was 1 / 1.

Codex uses its SDK's workspace-write preset. Claude enables its native Bash
sandbox, refuses startup without it, disables unsandboxed retries and enables its
[native read restriction](https://code.claude.com/docs/en/sandboxing#filesystem-isolation).
The harness owns its runtime and temporary-directory exceptions. DeepSeek uses
its native workspace-write permission preset. Octomate adds no outer sandbox,
custom runtime path list, or named permission profile.

Codex was checked with SDK and bundled CLI 0.162.1 through the actual `CodexInk`
thread and turn operations with a scripted local Responses provider. Restricted
→ full access → restricted turns confirmed that a warm thread regains its write
restrictions; resume also accepted the native preset. Claude was checked with SDK
0.2.165 and bundled CLI 2.1.294 through `ClaudeInk` with a scripted local Anthropic
provider. Its Bash probe requested sandbox bypass; outside writes and reads in
the user-data fixture remained denied. DeepSeek used its actual local sandbox
and filesystem providers from source version 0.2.1-alpha.2. Inkling used harness
0.34.0. No sandbox escalation was approved for the restricted runs.

Additional Codex and Claude controls passed for native temporary-file creation
and removal of Octomate server environment values. These are separate from the
16-check totals. The probes exercise skill-file reads and CLI effects, not
automatic skill discovery or a model's ability to follow arbitrary instructions.

The unit tests check policy wiring and mode changes using test doubles; the
native probes above are separate runtime evidence. Linux and Windows, unavailable
sandbox startup, resource quotas, controller-process isolation and remote
computer/browser authorization were not verified in this pass. MCP servers and
remote-device tools need their own user-bound authorization; the shell sandbox
does not confine them. Stronger isolation remains a separate deployment decision.

## Runtime commands

Permission changes belong to the agent tentacle. Channels use its permission
selection operation so the runtime can apply live changes where supported. See
the [agent interface](../api/tentacles/base.md#octomate.tentacles.agent.AgentTentacle.set_permission_mode) and
[permission modes](../usage/agents/permissions.md) for the contract and timing.

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
Browser command execution uses this graph entry for both Trunkline and IM
destinations. Results and approvals appear on the destination channel; the browser
receives completion after the graph finishes. See the
[command API](../api/schemas/commands.md) for the response contract.

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
`command_id`, a stable `delivery_id`, and optional raw `arguments`. The descriptor
decides whether a conversation is required. The server applies the execution access
checks below and resolves the descriptor from a fresh catalog. Browser attachments
are not supported yet; a nonempty `attachments` field is rejected before dispatch.

Execution answers with one JSON
[CommandOutcomeEvent][octomate.schemas.commands.CommandOutcomeEvent], with
`event_kind="command_outcome"` and the `CommandOutcome` in its `outcome` field,
once runtime cleanup and receipt persistence succeed. Direct and replayed feedback,
refusals and agent activity go out on the nerve while the command runs, for every
channel: the destination channel draws them, and a console watching the thread
reads them off its live socket. Clients render those events and use the outcome for
completion tracking, without displaying its content again. A failed execution
answers no outcome. Retrying the same delivery returns its saved outcome when
present and presents any saved feedback again. Agent activity is not replayed.
Authentication and request-validation failures remain non-2xx JSON responses.
Both Trunkline and IM HTTP execution receive gateway capabilities and the
suspender through Reflex.

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
| `requires_conversation` | Require a conversation unless the adapter explicitly permits a direct result before one exists. |
| `unavailable_reason` | Show a known command disabled with its explanation. The host refuses it before dispatch or a delivery receipt. |
| `unavailable_kind` | Distinguish a missing context prerequisite from a command the adapter does not implement. A channel can still supply its own handling. |

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

Known commands that the adapter cannot execute can remain visible as disabled
entries with a reason. Channels show that reason and the host refuses execution
before invoking the adapter or recording a delivery.

### Audit command ownership

For a new tentacle, inspect the SDK or protocol's actual command surface first.
Do not assume that commands in a terminal, desktop app and headless SDK are the
same. Keep a source reference and representative discovery and result fixtures.
Use dynamic discovery where offered; maintain an explicit app-command catalog
only for runtimes, such as Codex, whose app actions have no command registry.

Check each special command against these cases before enabling it:

| Case | Implementation requirement |
|---|---|
| Native inspection or state change | Use the native control API or command endpoint. Return direct feedback without creating a model run. An acknowledgement of queued work is not proof of completion. |
| Agent activity | Use the tentacle's existing recorded run stream and approval bridge through the Reflex command entry. Leave the ReAct core unchanged. Keep the workspace and subscription alive until the actual native work ends. |
| Octomate-owned selection or identity | Reuse the owner of model, effort, permission, project, thread or conversation state. A native-only setting that the next run overwrites is not a working control. Forks must create independent host and native histories. |
| Channel-owned control | Keep the descriptor neutral: report the native command's identity and runtime availability. Each channel decides whether to handle a known command through its own controls. Match built-in identities rather than skill names. Opening a picker must not send a runtime command or fabricate a thread or run. |
| Unsupported client feature | Keep the known command visible with a specific unavailable reason and an existing alternative where possible. Do not advertise it as executable before its handler works. |

An existing workspace, native session or channel thread is a prerequisite only
when the operation needs it. Discovery creates none of them. A direct inspection
may work before a conversation exists; a command that starts real agent activity
needs an owned conversation. Synced native transcripts remain read-only.

Preserve upstream identities and resolve plugin overrides through the runtime.
Use a stable definition ID for special handling when the protocol provides one,
rather than capturing every plugin with the same display name. Advertise only
capabilities the adapter implements, including attachments. Never dispatch an
unrecognized slash string as a fallback prompt.

Add a command matrix to the tentacle's usage page covering supported special
handling, disabled entries and omissions. Describe current behavior separately
from planned handlers. Test direct results without runs, real streamed runs,
refusals without dispatch, settings surviving the next run, scoped discovery,
and same-name plugin or skill collisions. Goal commands stay excluded until their
continuation lifecycle is integrated.

### Catalog and execution lifecycle

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
authenticated user and a stable, nonempty delivery ID. Execution on an existing
conversation requires that user's linked profile and access to the addressed chat
surface. IM execution also requires a linked profile before a conversation exists;
a signed-in user's new Trunkline composer does not. The selected agent must still
be enabled on the channel and own any selected conversation's route;
subagent conversations are not execution targets. These checks run once after
discovery, before dispatch or replay. A changed workspace, external session, model
or approval posture returns `stale`; missing access returns `unavailable`.

Execution refreshes the runtime catalog and validates command membership and
declared attachment support. Descriptors require a conversation by default.
An adapter may opt out for a command that can return a direct outcome before one
exists. In that context it must not start a run: the host closes and refuses an
unexpected event stream. Such execution creates no thread, conversation, run or
receipt, and delivery IDs do not deduplicate retries. It invalidates the agent's
catalogs after cleanup.

For existing conversations, execution holds the turn guard through stream cleanup.
Normal chat turns use the same guard even when gateway spells are disabled.
Busy conversations are refused immediately; execution never queues or retries.

Validation yields either a refusal/replay outcome or
`ValidatedCommand(surface, profile, descriptor)`. The surface is absent before a
conversation exists; the profile can also be absent for a new Trunkline composer.
For the validated command, prepare available user capabilities if needed, then
enter `commands.execute(agent, context, invocation, validated, delivery_id=..., ...)`
before leaving validation. The same turn guard spans both calls, and execution
does not repeat validation. User setup errors propagate and release the guard;
no receipt exists until execution starts, so that delivery can be retried.

For an existing conversation, before runtime dispatch the manager commits a command
receipt on the addressed chat surface. Its delivery ID must be unique among inbound
messages on that surface; use the same ID when retrying the same request. After checking current access and
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
    native_id: ClassVar[str | None] = None  # set when it serves a hook router
    in_process: ClassVar[bool] = False  # true if a live run parks on a human answer
    description: str = "What this agent is for, shown to summoning agents."

    def __init__(self, id: str, octomate: Octomate, *, config: MyAgentConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.gateway = config.gateway
        self.claims = dict(config.claims)
        self.permission_modes = (PermissionMode(value="ask", name="Ask"), ...)

    async def discover_models(self) -> None:
        models, claims = await self.probe_catalog()  # keyed provider:model
        self.set_model_catalog(models, claims)

    async def __aenter__(self) -> Self:
        await self.discover_models()
        return await super().__aenter__()  # builds the routes

    async def run(self, user_prompt=None, *, conversation_address, **kwargs):
        result = None
        async with self.run_stream_events(
            user_prompt, conversation_address=conversation_address, **kwargs
        ) as events:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
        if result is None:
            raise RuntimeError("run completed without a result")
        return result

    def run_stream_events(self, user_prompt=None, *, conversation_address, **kwargs):
        return ReactEventStream(
            self.iter_events(
                user_prompt, conversation_address=conversation_address, **kwargs
            )
        )
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

To support forking a driven conversation, implement `fork_session` to create an
independent native session. Call the base class's `fork` to carry the conversation
onto a new channel thread; it shares the lifecycle used for native-history forks.

## Native ingest

A runtime people also run by hand gets a hook router and a transcript stream.
Declare `native_id`, return the router from `routers()`, guard it with
`hook_guard` for the `hooks` scope and `hook_sender` to attribute rows, and write a
tailer that assembles turns from the streamed lines, committing each as an
`ExternalAgentRun` with its source range (byte offsets or event sequence numbers)
so a reconnect resumes after the last committed turn. Keep the runtime's
transcript read-only.
Add the matching installer under `cli/octomate_cli/tentacles/<runtime>/`, which
must stay importable without the server package. The three existing tailers show
three different transcript shapes.

Claude and Codex hooks reserve empty runs; the transcript owns messages and their ledger
bindings. Preserve order within each transcript independently of receipt time or
wall-clock timestamps. Parent and child transcripts retain their separate
conversations and spawning-call relationships. See the
[Claude ingest contract](../api/tentacles/hooks.md#octomate.tentacles.claude.ingest.ClaudeHookIngest).

All three native tailers publish turns on the nerve so an open console can show
them as they are assembled. DeepSeek's hooks carry no turn identity; its tail
creates the complete run from the session log.

After validating the stream's hello and resolving its sender, construct the
native address in the tentacle and keep its publisher open for the connection:

```python
address = ChannelAddress(
    channel_tentacle_id=self.native_id,
    chat_type="thread",
    chat_id=hello.session_id,
    user_id=sender.channel_user_id,
)
async with (
    self.driving(hello.session_id, native=True),
    self.octomate.nerve.publish(address) as impulses,
):
    await self.stream_attached(websocket, hello, sender, address, impulses)
```

The connection handler passes the address and publisher to the tailer. Use
[`NativeTurn`](../api/tentacles/hooks.md#octomate.tentacles.tailer.NativeTurn)
for the shared turn lifecycle, including prompt delivery, replay exclusion and
ledger settlement. Keep the runtime's transcript parsing in its own tailer.
The [`Nerve.publish` contract](../api/tentacles/nerve.md#octomate.nerve.Nerve.publish)
covers buffering and draining. Native ingest observes the runtime without waking
a reflex.

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
`match` in `octomate/app.py`. The loader enforces one block per agent type with
fixed routes. Outbound ACP harnesses mount no routes and may repeat.

## Tests

`tests/support/agents.py` has a scripted agent at the tentacle level and at the
model level; the reflex graph tests drive both. A harness tentacle's own tests
typically fake the SDK client and assert the launch options, the disabled
customisation, and the approval bridge, as `tests/agent/test_claude_tentacle.py`
and `test_codex_tentacle.py` do. Document the agent under `docs/usage/agents/`.

For live native ingest, subscribe to the nerve in tests and verify that a parent
turn reaches its native thread and settles only after its ledger rows are bound.
Cover silent replay of committed turns, restart of unfinished turns, failed
commits without a settlement signal, and subagent turns staying out of the parent
stream. Exercise the authenticated stream endpoint as well as the fold.
