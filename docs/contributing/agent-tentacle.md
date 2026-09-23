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
