# Octomate 🐙

**Relay and collect every chat you have with a coding agent — whichever harness made
it — then spread tentacles out to wherever you already work, and offer that history
and those tools wherever you want them.**

Three things, in that order:

- **Collect.** Claude Code, Codex, DeepSeek Harness, or a run you drove from chat —
  every turn lands in one record, including the sessions you start yourself in your own
  terminal or app.
- **Spread.** The same thread reaches Slack, Lark, Discord and the web console,
  rendered natively on each. You go on working where you already work.
- **Offer.** That history, and the tools built on it, are available from any of those
  surfaces — searchable mid-run, resumable later, handed to a different agent when the
  one that started is not the one that should finish.

> ⚠️ **Early development** — APIs and architecture are subject to change.

**Documentation: [kalynnka.github.io/octomate](https://kalynnka.github.io/octomate/)** —
installation and an agent setup brief, a [Tentacles catalog](https://kalynnka.github.io/octomate/tentacles/)
with enablement guides, and the concepts behind the design.

---

## It does not ask you to change how you work

Keep running Claude Code, Codex or DeepSeek Harness (`dsh`) the way you already do: your
terminal, your flags, your harness, your choice of agent. There is no wrapper to launch
through and no session to start somewhere else first.

Octomate follows the transcript from where it left off and takes the hook stream
alongside it, so every turn — the prompt, the tool calls, the answer, and any subagents
it spawned — lands in the same record as the work you drive from chat. One command per
harness sets it up:

```bash
octomate claude hooks install
octomate codex hooks install
octomate deepseek hooks install --bridge <harness>/packages/hooks/hooks-claude-code
```

What that buys you is everything downstream of having the session at all: read it back
later, resume it from a chat thread, or hand the same context to a different agent
because the one you started with is not the one that should finish.

## Channel tentacles

A run is an event stream that channels consume, rather than text one channel formats.
The same turn renders natively wherever it lands — streaming text, tool cards, todo
lists, approval buttons — and the thread it belongs to is the same thread on every
surface.

| Channel | Transport | Status |
|---|---|---|
| **Slack** | Slack Bolt, Socket Mode | ready |
| **Lark / Feishu** | lark-oapi, WebSocket long connection | ready |
| **Discord** | discord.py, Gateway WebSocket | ready |
| **Trunkline** | the web console, over `/api/trunkline` | 🚧 preview |

Every one of these dials out, so none of them needs an inbound port. A port is only
needed for what you point at Octomate yourself: the native-session hook routers, the
console, and OAuth callbacks.

A channel is a `Chromo` (platform events in), an `Ink` (what sends and edits), and a
set of `Feelers` (how a run is drawn), so adding one does not touch the graph or the
agents. Channels are keyed by instance, not by platform, so two Lark apps are two keys
in `tentacles.yaml` and two separate sets of threads.

Which is worth having when:

- You start something on your laptop and want to keep reading it on your phone.
- Someone asks a question in a team channel and the agent already knows what you changed
  this morning, because it recorded the session you ran in your terminal.
- A run is going to take a while and you have something else to say: threads are
  independent, so open another one and get on with it while the first works.
- A group thread turns into something personal: `scheme` moves the brief into your DMs
  and the conversation continues there.
- The work turns out to belong to someone else: whoever picked it up `summon`s the agent
  you trust for that kind of work, handing over a brief rather than a pasted transcript.

## Agent tentacles

The other half of the pair. Each agent tentacle wraps somebody else's harness — Octomate
drives them, it does not reimplement them.

| Agent | Runtime | Native session ingest | Notes |
|---|---|---|---|
| **claude** | Claude Agent SDK | ✅ hooks + transcript tailer | runs locally |
| **codex** | openai-codex SDK | ✅ hooks + rollout tailer | |
| **deepseek** | DeepSeek Harness (`dsh`), over its `/api` gateway | 🚧 WIP | hooks and event tailing work; no Octomate tools in a driven run yet |
| **inkling** | in-process pydantic-ai agent | — | any pydantic-ai supported provider or model; every MCP connector's tools, as the person who asked |

The first three feed the native-session ingest above, so a session started in your
terminal and a run summoned from Slack are the same kind of thing afterwards. `inkling`
is the one that runs in-process: the chat-side generalist, and the fallback for any
model pydantic-ai can reach.

## Trunkline — the web console 🚧

> **Work in progress.** Usable, and changing week to week: panels, API shape and
> design are all still moving. Treat it as a preview rather than a stable surface.

Trunkline is Octomate's own web console, and the one channel that is not somebody
else's chat app. It is both an entry and a reader: threads on the `trunkline` channel
are yours to start and continue from the browser, and every *other* channel's threads,
and every native session the tailers picked up, are readable there too. It is also
where you register, issue API tokens, link channel profiles and install MCP connectors.

React + TypeScript + Vite. See [`trunkline/README.md`](trunkline/README.md).

## Approvals and questions are actions, in batches

Most tools give you a global switch — approve everything, or approve nothing. Octomate
raises **actions** instead. One action is exactly one thing you are asked: one approval,
or one question. Never two bundled into a card you have to read twice.

Actions come up as a **batch** — everything a turn is waiting on, together — so a turn
that needs three tools and an answer arrives once rather than as four interruptions in a
row. Each action carries its own card, whoever answered it, and when it resolved.

Actions are persisted before they are asked and rehydrated from the platform callback
when you press the button, so the run is suspended in the database rather than parked
in memory.

## Think it through together, then ship it

- **Search what was already said** — every thread the person you are talking to has
  spoken in, on any of their linked accounts, queryable mid-run.
- **Split a topic without losing it.** `teleport` carries the history into its own
  sub-thread, so a tangent gets its own room instead of burying the main one.
- **Work in a project.** A thread bound to a project runs in its own git workspace,
  forked from a mirror, saved after every turn. Nothing reaches the upstream until a
  person asks for a pull request.
- **Hand the result to something that can land it.** Brainstorm with colleagues in the
  channel, then pass the thread to a coding agent as a brief.

---

## How it works

```
  Slack / Lark / Discord / Trunkline         a session you run yourself
             |                                       |
             v                                       v
      ChannelTentacle                      hook router + transcript tail
             |                                       |
             +-------------------+-------------------+
                                 v
                             Octomate
                                 |
                                 v
                           reflex graph
        Awake -> Route -> React -> Handoff / Teleport / Scheme
                                 |
                                 v
                           AgentTentacle
                  claude / codex / deepseek / inkling
                                 |
                     +-----------+---------------+
                     v                           v
               event stream              batch of actions
                     |                approvals and questions
                     v                           |
                the channel <----- cards --------+
```

The graph is declared, not dispatched: every edge comes from a node's own return
annotation, so a transition is written where it happens. A run ends either with a result
or suspended on a batch of actions that has not come back yet — and a suspended run is a
row, which is why restarts are survivable.

## Installation

Two halves. The **server** (`octomate` on PyPI, or a release checkout) runs the
database, the channels and the driven agents. The **client** (`octomate-cli`) lives on
every machine where you run a coding agent and forwards its sessions.

```bash
# the server, on macOS: an isolated install and a LaunchAgent the CLI manages
uv tool install octomate-cli
octomate service init --prepare
```

```bash
# the client, wherever a coding agent runs
uv tool install octomate-cli
octomate configure --url https://octomate.example.com --token '<api-token>'
octomate claude hooks install && octomate claude mcp install
```

Both halves are one package: `octomate-cli` is the operator tool on the server host
and the client everywhere else. macOS is the route that runs in production; the
[Linux](https://kalynnka.github.io/octomate/installation/linux/),
[Windows](https://kalynnka.github.io/octomate/installation/windows/) and
[Docker](https://kalynnka.github.io/octomate/installation/docker/) recipes are
derived from it and not yet validated, and say so. The
[server](https://kalynnka.github.io/octomate/installation/quickstart/) and
[client](https://kalynnka.github.io/octomate/installation/clients/quickstart/) quick
starts are the short versions, and there is a
[brief](https://kalynnka.github.io/octomate/installation/quickstart/#agent-tldr) you can hand to
your own agent to do the setup.

## Configuration

A deployment is a **config home**: one directory, one flat YAML per subsystem, chosen
from `$OCTOMATE_HOME`, then `./.octomate/config/`, then `~/.octomate/config/`.

```
.octomate/
  octomate.db            the deployment's data
  cli.toml               the client's own config — not the server's
  config/
    octomate.yaml        host, port, db_url
    tentacles.yaml       every agent, channel and MCP connector, keyed by id
    auth.yaml            local account settings
    projects.yaml        code locations an agent may work in
    providers.yaml       LLM credentials for inkling
    observability.yaml   logging, logfire
    oauth.yaml           the callback origin and the key that encrypts stored tokens
```

Agents, channels and MCP connectors are all **tentacles**, declared in one map with
`type` selecting the implementation. Nothing is defaulted on, and no model is chosen
for you.

```yaml
tentacles:
  claude:
    type: claude
  slack:
    type: slack
    app_id: A0123456789
    agents: [claude]
```

Secrets can be supplied in YAML or environment variables; `.env` is optional.
Environment variables use the `OCTOMATE__` prefix and `__` between levels, so
`OCTOMATE__TENTACLES__SLACK__BOT_TOKEN` sets `tentacles.slack.bot_token`. The
[configuration page](https://kalynnka.github.io/octomate/installation/configuration/)
has the precedence rules and every block.

## Development

```bash
uv sync
uv run pytest
uv run ruff format <paths> && uv run ruff check <paths>
uv run --no-sync mkdocs serve --livereload --dev-addr 127.0.0.1:8001
```

Ruff is the gate: its configured rule set in `pyproject.toml` is what "clean" means.
Foreign keys are enforced on every connection, in tests too, so a row needs its parents
to exist. `AGENTS.md` holds the engineering rules; the
[Contributing](https://kalynnka.github.io/octomate/contributing/) tab has the
extension points, with a skeleton for a new channel or agent.

## Anatomy

The codebase keeps an octopus metaphor, and these are the words it uses:

| Body part | Concept | What it is |
|---|---|---|
| **Octomate** 🐙 | `octomate/base.py` | The coordinator. Owns every tentacle, and the managers they share. |
| **Tentacle** 🦑 | `ChannelTentacle` | One per configured channel, keyed by instance: Slack, Lark, Discord, Trunkline. |
| **Agent tentacle** 🧠 | `AgentTentacle` | One per agent: claude, codex, deepseek, inkling. |
| **Reflex** ⚡ | `octomate/reflex/` | The graph a signal runs through, from waking to a result or a suspension. |
| **Feeler** 🫧 | `feelers/` | The view. Decides how a streamed run event is rendered on a channel — timeline, segments and markdown, plus the cards you answer. |
| **Ink** 🖊️ | per-channel client | What actually sends, edits and uploads on the platform. |
| **Spill** 💧 | `SpillStore` | Where an oversized tool return goes, so it is read back on demand instead of re-sent every turn. |
| **Awake** 🌊 | `AwakeSignal` | What arrives: a message, or a batch of answered actions coming back. |

## License

Copyright © 2026 Lu Hui.

Octomate is free software under the [GNU Affero General Public License v3.0](LICENSE):
use it, change it, and run it as you like. If you offer a modified version to others over
a network, the AGPL asks you to offer them its source too.
