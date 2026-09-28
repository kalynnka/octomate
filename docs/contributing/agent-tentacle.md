# Adding an agent tentacle

An agent tentacle wraps a runtime so Octomate can drive it: run a turn for a
conversation, stream what happens in Octomate's event vocabulary, raise approvals
and questions as deferred requests, and advertise its models. Inkling is the
smallest complete example; Claude Code is the one to read for a subprocess harness.

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

The optional `discover_commands` and `execute_command` hooks use the
[command schemas](../api/schemas/commands.md). Their defaults report unsupported
without starting a turn. These are extension hooks only: HTTP endpoints, channel
entrypoints and runtime adapters are not wired yet.

Both hooks receive a `CommandContext` resolved by the caller: authenticated user,
originating channel address, effective workspace, conversation, model and approval
posture. `conversation=None` represents a new composer. Discovery must not send a
model prompt or create a visible turn. If the runtime requires a session, return
an unavailable catalog explaining the prerequisite instead of creating one
silently.

Implement discovery from the running backend's registry. `AgentCommandCatalog`
contains the owning `agent_id` and one `catalog`, whose state is ready, loading,
unsupported, unavailable or failed. A ready catalog has one list of `entries`:
commands and skills share the same channel-facing contract. Its `limitations`
explain gaps such as Codex exposing skills without a native command catalog.
A successful discovery with no entries is an empty ready catalog.

Channels use only these `CommandDescriptor` fields:

| Field | Channel use |
|---|---|
| `id` | Submit the opaque selection to the owning tentacle. IDs must be unique within the scoped catalog; names may repeat. |
| `name` | Show and filter the upstream command or skill name. |
| `description` | Show the upstream description. |
| `argument_hint` | Show a free-form input hint when supplied; never parse it as an argument schema. |
| `accepts_attachments` | Offer attachments only for `true`. `false` means unsupported; `None` means unspecified. |

Extend `CommandDescriptor` with typed attributes in the owning tentacle's schema
module. For example, a Codex adapter can retain the exact skill path:

```python
class CodexCommandDescriptor(CommandDescriptor):
    path: Path = Field(description="The upstream skill path used for invocation.")
```

Claude aliases or a DSH definition ID belong on their respective descriptor
subclasses in the same way. Map the runtime's invocation identity to the common
`id`; channels must not construct a runtime path or slash line from the display
name. Keep all standard fields' meanings unchanged when extending the model.

Construct catalogs with these concrete descriptor instances. The `entries` field
uses Pydantic's `SerializeAsAny` to preserve declared subclass attributes when
serializing the catalog. A channel may parse the result using the base contract
and ignore the additional fields. A tentacle that rehydrates serialized metadata
must validate it with its own concrete model: base-model parsing intentionally
does not reconstruct runtime subclasses. Invocation requests carry only the
selected ID, raw arguments and resolved attachments, not a client-supplied copy
of the runtime descriptor.

`execute_command` receives explicit intent and raw arguments. It returns a
`CommandResult` with existing message segments, a `CommandError`, or a lazy
`ReactEventStream` for an entry that runs the agent. For an agent run, forward the
supplied suspender and capabilities through the adapter's normal approval and
streaming implementation. Keep direct controls out of `run` and preserve raw
arguments without injecting chat context. The default never interprets slash text
or forwards unsupported commands to the model.

There is no fixed execution-kind field on a descriptor. Decide behavior for each
invocation: DSH's `/plan off` changes state, while `/plan <message>` also submits
agent input. If a command produces both control feedback and agent activity, its
adapter must expose both through the existing stream events.

The caller must authorize the target, revalidate catalog membership and attachment
support, deduplicate delivery and hold the active-turn guard through stream cleanup.
These hooks do not enforce those host responsibilities. A native transcript handle
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
5. Raise approvals and questions through the channel's `present_actions`, wait up
   to `approval_timeout`, and answer the runtime. Mark the batch expired on
   timeout so the run can finish.
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
