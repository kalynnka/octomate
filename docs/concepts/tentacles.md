# The tentacle model

A tentacle is a lifecycle component the host owns: it has an id, a reference back
to Octomate, a log colour, the vendor loggers it claims, optional routers, and an
async context the host enters and exits. It is not a message dispatcher. Three
families extend it.

## How a `type` becomes a class

There is no central registry. The chain is:

1. A config model with `type: Literal["..."]`, a member of the discriminated union
   `TentacleConfigVariant`, which pydantic validates the `tentacles:` map against.
2. The app factory's `match` over the validated block: agents construct directly,
   channels go through `build_channel`, connectors through `build_mcp`.
3. `Octomate.connect`, which refuses a duplicate id, assigns the log colour, files
   an MCP tentacle with the MCP manager, and mounts the tentacle's routers.

Platform modules are imported inside the builders, so importing the package does
not cost every vendor SDK. Adding a type touches the union, the builder or the
factory, and the config package's exports; [Contributing](../contributing/index.md)
walks through each family.

## Agent tentacles

`AgentTentacle` is abstract over two calls, `run` and `run_stream_events`, both
taking the conversation's address and thread, the prompt or the deferred results
to resume with, a model and effort, an interactive flag, and the capabilities to
mount. A run yields the harness's events translated into Octomate's stream
vocabulary, ending in a run result whose output is text, segments, or a set of
deferred requests. The base class provides the rest: claims and routes, model
discovery hooks, session counters, the resumed-prompt fallback for a runtime that
takes no tool result back, and the project lookup for the run's workspace.

Each harness tentacle also serves its native hook router and transcript stream,
authenticated by the `hooks` scope, and owns a tailer that assembles turns from the
stream. Inkling has none of that; it is the harness.

## Channel tentacles

`ChannelTentacle` is concrete. Everything it needs comes from the ink and chromo it
is built with, and two class-level declarations say what the platform can do:
`thread_strategy`, whether an inbound threaded message continues its thread without
triage, and `surfaces`, whether the bot can open a sub-thread or a direct message.
The second is declared rather than probed because a spell has to know before it
runs.

`ingest` is the whole inbound pipeline: decode, resolve the sender, download images,
resolve the thread, drop a redelivered message, record the ledger row, apply the
mention gate, kick. Trunkline reproduces it inline because its input is an HTTP
request rather than a socket event.

## MCP tentacles

`McpTentacle` is a description: a label, an upstream URL, an auth kind,
instructions, and whether it is serving. Three generic variants come from
configuration; a bespoke one exists only where a class must override those, as
Slack does to serve its connector as the same person the channel talks to.

## Lifecycle

Startup enters agents and connectors first, concurrently, then channels, each with
a thirty-second budget; a failure or a hang is logged and the rest are served.
Shutdown exits channels first so nothing ingests into a closed session. An agent's
routes are built when it enters and cleared when it exits, which is why model
discovery must finish before any channel accepts a turn.
