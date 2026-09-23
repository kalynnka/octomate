# Architecture

Octomate is a **relay**. On one side, the harnesses people already run: Claude
Code, Codex, DeepSeek Harness, and its own in-process Inkling. On the other, the
places they already talk: Slack, Lark, Discord, QQ, the console. In the middle, one
record of every conversation, one graph that decides where a message goes, and one
set of tools every agent gets. Trunkline's status bar calls the server the relay for
the same reason.

```text
  Slack / Lark / Discord / QQ / Trunkline      a session you run yourself
                 |                                      |
                 v                                      v
         ChannelTentacle                     hook router + transcript tail
                 |                                      |
                 +------------------+-------------------+
                                    v
                                Octomate
                     managers: threads, conversations,
                     actions, users, workspaces, MCP
                                    |
                                    v
                             the reflex graph
              Awake -> Route -> React -> Handoff / Teleport / Scheme
                                    |
                                    v
                              AgentTentacle
                    claude / codex / deepseek / inkling
                                    |
                    +---------------+----------------+
                    v                                v
              event stream                  a batch of actions
                    |                                |
                    v                                v
            the channel's feelers  <----- cards ----+
```

## The pieces

**Octomate** is the coordinator, and also the FastAPI application. It owns the
managers, which are the only things that touch the database, and the tentacles, in
connection order. Startup reconciles the project registry, probes the filesystem for
the workspace fork mechanism, enters the MCP transport, then enters every tentacle:
agents and connectors first, because a channel must not accept a turn before model
discovery, then channels. Shutdown is the reverse, so nothing ingests into a closed
session. A tentacle that hangs on start is dropped after thirty seconds and the rest
are served.

**Tentacles** are lifecycle components the host owns, not message dispatchers. Three
families share one base: a `ChannelTentacle` holds a platform connection, an
`AgentTentacle` wraps a runtime, an `McpTentacle` describes a connector users can
install. A tentacle can be more than one at once, which is how Slack's channel is
also a connector. [The tentacle model](tentacles.md) covers the hierarchy.

**The reflex graph** is what a signal runs through from waking to a result or a
suspension. Its edges are declared by each node's return type rather than by a
dispatcher, so a transition is written where it happens. [The reflex graph](reflex.md).

**Feelers** are the view: how a run is drawn on a platform, and how the cards you
answer are drawn and read back. [Feelers](feelers.md).

**Persistence** is Arcanus over SQLAlchemy over SQLite: typed transmuters that are
both the validated object and the row, so managers never juggle two
representations. [Persistence](persistence.md).

## Proxies

The word appears in a few places with a few meanings, worth separating:

- The **MCP proxy**: Octomate forwards tool calls to the servers a user installed,
  as that user, without re-exporting their tools. [MCP proxy](../usage/mcp/proxy.md).
- A **run proxied to a channel**: a harness's native event stream translated into
  Octomate's event vocabulary and drawn by feelers. That is the adapter layer inside
  each agent tentacle.
- Arcanus **proxying** ORM attributes through the transmuter, which is what lets a
  schema object read and write its row.
- An **inbound reverse proxy** in front of the server, and an **outbound proxy** for
  provider traffic, both deployment concerns. [Networking](../installation/networking.md).

[Vocabulary](vocabulary.md) is the glossary of the words the code uses.
