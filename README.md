# Octomate 🐙

**Relay and collect every chat you have with a coding agent — whichever harness made
it — then spread tentacles out to wherever you already work, and offer that history
and those tools wherever you want them.**

Three things, in that order:

- **Collect.** Claude Code, Codex, DeepSeek Harness, or a run you drove from chat —
  every turn lands in one record, including the sessions you start yourself in your own
  terminal or app.
- **Spread.** The same thread reaches Slack, Lark, the web console and QQ, rendered
  natively on each. You go on working where you already work — and more channels are
  on the way.
- **Offer.** That history, and the tools built on it, are available from any of those
  surfaces — searchable mid-run, resumable later, handed to a different agent when the
  one that started is not the one that should finish.

> ⚠️ **Early development** (v0.0.1) — APIs and architecture are subject to change.

---

## It does not ask you to change how you work

Keep running Claude Code, Codex or DeepSeek Harness (`dsh`) the way you already do: your
terminal, your flags, your harness, your choice of agent. There is no wrapper to launch
through and no session to start somewhere else first.

Octomate follows the transcript from a byte offset and takes the hook stream alongside
it, so every turn — the prompt, the tool calls, the answer, and any subagents it spawned
— lands in the same record as the work you drive from chat. One command per harness sets
it up:

```bash
octomate claude hooks install
octomate codex hooks install
octomate deepseek hooks install
```

What that buys you is everything downstream of having the session at all: read it back
later, resume it from a chat thread, or hand the same context to a different agent
because the one you started with is not the one that should finish.

## Channel Tentacles

A run is an event stream that channels consume, rather than text one channel formats. The
same turn renders natively wherever it lands — streaming text, tool cards, todo lists,
approval buttons — and the thread it belongs to is the same thread on every surface.

| Channel | Transport | Status |
|---|---|---|
| **Slack** | Slack Bolt, Socket Mode | ready |
| **Lark / Feishu** | lark-oapi, WebSocket long connection | ready |
| **Trunkline** | the web console, over `/api/trunkline` | 🚧 WIP |
| **QQ (NapCat)** | NapCat, OneBot WebSocket | 🚧 WIP |

Every one of these dials out, so none of them needs an inbound port. A port is only
needed for what you point at Octomate yourself: the native-session hook routers, and
OAuth callbacks.

**QQ (NapCat)** is named for its bridge rather than for QQ, because that is what it
really is: NapCat is a community reimplementation on top of NTQQ, not a vendor SDK like
the others. It sits apart for that reason, and it has not been exercised in a while —
treat it as unverified.

**More channels are coming.** A channel is a `Chromo` (platform events in), an `Ink`
(what sends and edits), and a set of `Feelers` (how a run is drawn), so adding one does
not touch the graph or the agents.

Channels are keyed by instance, not by platform, so two Lark apps — or two consoles —
are two keys in `channels.yaml` and two separate sets of threads.

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

## Agent Tentacles

The other half of the pair. Each agent tentacle wraps somebody else's harness — Octomate
drives them, it does not reimplement them.

| Agent | Runtime | Native session ingest | Notes |
|---|---|---|---|
| **claude** | Claude Agent SDK | ✅ hooks + transcript tailer | runs locally; 🚧 an SSH transport for running on another host is WIP |
| **codex** | openai-codex SDK | ✅ hooks + rollout tailer | |
| **deepseek** | DeepSeek Harness (`dsh`), over its `/api` gateway | ✅ hooks + event tailer | attaches to a `dsh web` you already run, and starts one only if nothing answers |
| **inkling** | in-process pydantic-ai agent | — | any pydantic-ai supported providers or models; MCP toolsets, per-user OAuth integrations |

The first three feed the native-session ingest above, so a session started in your
terminal and a run summoned from Slack are the same kind of thing afterwards. `inkling`
is the one that runs in-process, and it is the chat-side generalist rather than the
point of the project.

Models are advertised through **claims** — what a route is for, and which thinking
efforts it accepts. A model with no claim is not summonable, so what an agent offers is
config rather than a hardcoded list. Nothing is defaulted: an agent names the models you
hold keys for, or it is absent.

## Trunkline — the web console 🚧

> **Work in progress.** Usable, and changing week to week: panels, API shape and
> design are all still moving. Treat it as a preview rather than a stable surface.

Trunkline is Octomate's own web console, and the one channel that is not somebody
else's chat app. One screen, five panels — threads sidebar, control rail, review
dossier, chat ledger, timeline — over a status bar.

It is both an entry and a reader. Threads on the `trunkline` channel are yours to
start and continue from the browser; every *other* channel's threads, and every
native session the tailers picked up, are readable there too. So it is where you go
to see the terminal session you ran an hour ago next to the Slack thread a colleague
opened about it.

React + TypeScript + Vite, using the Lonetrail design system. It is served
separately from the API and proxies `/api` and `/oauth` back to it:

```bash
cd trunkline
pnpm install
pnpm dev              # http://localhost:5173, proxying to 127.0.0.1:8000
```

With the API down the status bar shows `relay offline` and the ledger panels stay
empty. See [`trunkline/DESIGN.md`](trunkline/DESIGN.md) for the visual world and
[`trunkline/PRODUCT.md`](trunkline/PRODUCT.md) for what it is meant to do.

## Approvals and questions are actions, in batches

Most tools give you a global switch — approve everything, or approve nothing. Octomate
raises **actions** instead. One action is exactly one thing you are asked: one approval,
or one question. Never two bundled into a card you have to read twice.

Actions come up as a **batch** — everything a turn is waiting on, together — so a turn
that needs three tools and an answer arrives once rather than as four interruptions in a
row. Each action carries its own card, whoever answered it, and when it resolved, which
is what makes "who approved that" a row rather than a scroll through the channel.

Actions are persisted before they are asked and rehydrated from the platform callback
when you press the button, so none of this is tied to a process staying alive. Restart
in the middle of a batch and the buttons still land the run where it left off, because
the run is suspended in the database rather than parked in memory.

## Think it through together, then ship it

The thread is where the work gets decided, so the tools that matter there are the ones
for thinking with other people:

- **Search what was already said** — this thread's chat ledger and the conversation's
  model ledger, both queryable mid-run.
- **Split a topic without losing it.** `teleport` carries the history into its own
  sub-thread, so a tangent gets its own room instead of burying the main one.
- **Hand the result to something that can land it.** Brainstorm with colleagues in the
  channel, then pass the thread to a coding agent as a brief.

---

## How it works

```
  Slack / Lark / Trunkline / QQ    a session you run yourself
             |                               |
             v                               v
      ChannelTentacle              tailer + hook router
             |                               |
             +---------------+---------------+
                             v
                         Octomate
                             |
                             v
                       reflex graph
   Awake -> Route -> React / Handoff / Teleport / Scheme
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

## Quickstart

**Requirements:** Python 3.13+, [uv](https://docs.astral.sh/uv/). The database is a
SQLite file under `.octomate/`, so there is nothing to stand up first.

### 1. Collect your own sessions

The smallest useful Octomate. No API key, no chat platform, no tokens — it records the
Claude Code sessions you already run.

```bash
uv sync
uv run alembic upgrade head
mkdir -p .octomate
```

Declare one agent — that is the whole config:

```bash
cat > .octomate/agents.yaml <<'YAML'
agents:
  claude:
    models: [opus, sonnet]
    claims:
      opus:
        ability: Deep, multi-step engineering across a repository.
      sonnet:
        ability: Everyday software tasks and mid-sized changes.
YAML
```

A configured `claude` serves a hook router, and that router authenticates — so it needs
a credential before it will boot. Order matters here: the first call generates one and
exports it, the second sees it resolve and appends the *same* line.

```bash
eval "$(octomate secret)"     # this shell
octomate secret >> ~/.zshrc   # and every later one (zsh)
```

The secret stays in the environment, never in the config home — the server and the
hooks both read `OCTOMATE__SECRET` from there.

Then serve it and point Claude Code at it:

```bash
uv run octomate serve --tmux
octomate claude hooks install
```

Start a Claude Code session anywhere — a terminal, the VSCode extension, the desktop
app. Every turn is now recorded: prompt, tool calls, answer, subagents. Nothing about
how you work changed.

### 2. Relay it to Slack

This is the part worth having. The thread you started in your terminal is now readable
from Slack, and answerable there too.

Create a Slack app with Socket Mode on, then declare the channel — structure in the
config home, secrets in `.env`:

```bash
cat > .octomate/channels.yaml <<'YAML'
channels:
  slack:
    type: slack
    app_id: A0123456789
    mention_only: true
    agents:
      - agent: claude
        model: sonnet
      - agent: claude
        model: opus
YAML

cat >> .env <<'ENV'
OCTOMATE__CHANNELS__SLACK__BOT_TOKEN=xoxb-...
OCTOMATE__CHANNELS__SLACK__APP_TOKEN=xapp-...
ENV
```

Restart, and `@`-mention the bot in a channel or DM it. `agents[0]` is what answers by
default; the rest are summon candidates. Lark is the same shape with `type: lark` and an
`app_id`/`app_secret` pair.

### 3. Add the web console

Optional, and no platform account needed — `type: trunkline` alongside the Slack block:

```yaml
  trunkline:
    type: trunkline
    agents:
      - agent: claude
        model: sonnet
```

```bash
cd trunkline && pnpm install && pnpm dev   # http://localhost:5173
```

### Running it

`octomate serve --tmux` serves in a detached tmux session and attaches to it, creating
it if it is not already running — so the same command is both "start" and "go look at
it". Octomate is meant to outlive the terminal that started it: channels hold their
sockets open, and the tailers keep watching for native sessions started somewhere else
entirely. `--reload` restarts on changes under `octomate/`.

Run it on the machine you work on. The tailers read transcript files off local disk and
agents run in your checkouts, so Octomate is not a service you host away from your
filesystem.

## Configuration

A deployment is a **config home**: one directory, one flat YAML per subsystem. Each
file's top-level keys are config field names, so changing a channel touches
`channels.yaml` and nothing else.

```
.octomate/
  octomate.yaml        host, port, secret, mcp_path, db_url
  agents.yaml          claude, codex, deepseek, inkling
  channels.yaml        slack, lark, napcat, trunkline
  users.yaml           registered humans and their per-channel ids
  projects.yaml        code locations an agent may run in
  providers.yaml       LLM credentials
  integrations.yaml    per-user OAuth connectors
  mcp.yaml             vendor MCP servers on one operator token
  observability.yaml   logging, logfire
  oauth.yaml           the key that encrypts stored tokens
```

The home is **chosen, not merged** — the first of these that applies:

| | Where | When |
|---|---|---|
| 1 | `$OCTOMATE_HOME` | Set. Used as given, even if empty |
| 2 | `./.octomate/` | It holds at least one of the files above |
| 3 | `~/.octomate/` | Otherwise — one deployment for the machine |

Beneath whichever wins sit the packaged defaults in `octomate/config/defaults/`,
layered per top-level key: a home that declares `channels:` replaces the default
`channels:` whole and inherits the rest. Every default file is commented rather than
set, so it doubles as the reference for what a key means.

Nothing is defaulted on, and no model is chosen for you. Every agent is opt-in and
must name at least one model; every channel must name at least one agent route. A
model picked on your behalf would be a route that boots fine and 401s on first use.

Channels are keyed by instance id with `type` selecting the platform, so one platform
can be mounted more than once — two Lark apps are two keys. That key is the channel
tentacle id everywhere else: what a `users[]` profile names, and what a thread
records as its origin.

Secrets stay out of the home. `.env` in the working directory and the process
environment both override it, using `OCTOMATE__` with `__` as the nested delimiter —
`OCTOMATE__CHANNELS__SLACK__BOT_TOKEN` sets `channels.slack.bot_token`.

### Native session hooks

Configuring `agents.claude`, `agents.codex` or `agents.deepseek` serves that agent's hook router (`/hooks/claude`, `/hooks/codex`, `/hooks/deepseek`) for native sessions to POST their prompts and answers into. Those routes write straight into thread history, which agents read back, so they authenticate — Octomate refuses to boot without a credential.

Set the credential up, then point your clients at it:

```bash
eval "$(octomate secret)"                        # this shell
octomate secret >> ~/.zshrc                      # and every later one (zsh)
octomate claude hooks install                    # merges handlers into ~/.claude/settings.json
octomate codex hooks install                     # merges handlers into ~/.codex/hooks.json
octomate deepseek hooks install --bridge <path>  # writes $DSH_HOME/octomate-hooks.json + a patch row
```

`octomate secret` prints one line — `export OCTOMATE__SECRET=…` — and writes nothing; where your login environment comes from is yours to know. Sessions only ever read the **environment**, and they are separate processes that never see your config home, so that line is the bridge, and it has to reach whatever launches them.

`~/.zshrc` covers interactive zsh, which is what VSCode resolves the environment from; use `~/.zshenv` instead if you want non-interactive shells to have it too, and on another shell put the line wherever that shell would find it. Either way an environment is captured when a process starts: shells already open keep the one they had, and a GUI client (VSCode, the desktop app) grabs it when *it* launches — so restart them before expecting the hooks to carry the secret.

Native sessions can also *route*: with `agents.<agent>.native_gateway` on (the default), a session in your terminal reaches the same gateway spells the driven agents get — over `/gateway/mcp`, carrying the bearer plus a static `X-Octomate-Client` header written at install time. That surface authenticates the bearer, not the caller: the client header is attribution. Any holder of `OCTOMATE__SECRET` can already forge any session's ledger through the hook pipe; the gateway adds one power to the same credential — outbound sends and handoffs to real channels. Same trust domain (the operator's machines), same mitigations (per-deployment secret, HTTPS off-box), plus the `native_gateway` and per-connection `gateway` flags. A native session is anonymous, so its spells light up only where a `users:` entry claims the shared native profile — `profiles: {claude-native: {channel_user_id: native}}` — beside real accounts: a single-operator assumption, on purpose.

Point the runtimes' native sessions at it with the `mcp` commands — static MCP client config, written once:

```bash
octomate claude mcp install    # mcpServers.gateway in ~/.claude.json (--scope project: ./.mcp.json)
octomate codex mcp install     # [mcp_servers.gateway] in ~/.codex/config.toml
octomate deepseek mcp install  # a dsh-mcp-client row in $DSH_HOME/cordis.patch.yml
```

Unlike the hooks — whose scripts resolve the address and credential from the environment when each hook fires — a static entry is read by the runtime itself, so `mcp install` resolves both once and writes them into the file: the file holds the literal credential, and rotating it means re-running install. (Codex differs: its entry names `OCTOMATE__SECRET` as a `bearer_token_env_var`, resolved from each session's environment, so the secret must be exported in the shell profile.)

---

## Project structure

```
.
+-- main.py                    # Builds the FastAPI app - wires agents and channels
+-- octomate/
|   +-- base.py                # Octomate: the coordinator every tentacle is connected to
|   +-- reflex/                # The run graph - nodes, state, and the suspender
|   +-- tentacles/
|   |   +-- agents/             # claude, codex, deepseek, inkling - adapters, ingest, tailers, hooks
|   |   `-- channels/           # slack, lark, napcat, trunkline - and the feelers they draw with
|   +-- capabilities/          # Tools agents are given: gateway, ask, todos, history, harness
|   +-- managers/              # Threads, conversations, deferred actions, spills, users
|   +-- schemas/               # Pydantic/Arcanus transmuters - the persisted domain types
|   +-- models/                # SQLAlchemy ORM models behind those schemas
|   +-- config/                # The config home, and the settings it validates into
|   |   `-- defaults/           # Packaged defaults - commented reference for every key
|   `-- oauth/                 # Device and authorization-code flows, per user
+-- cli/octomate_cli/          # `octomate ...` - the client half, installable alone
+-- trunkline/                 # The web console (React + Vite)
+-- migrations/                # Alembic
`-- tests/
```

## Development

```bash
uv run pytest
uv run ruff format <paths> && uv run ruff check <paths>
```

Ruff is the gate: its configured rule set in `pyproject.toml` is what "clean" means.
Foreign keys are enforced on every connection, in tests too, so a row needs its parents
to exist.

Tracing goes to [Logfire](https://logfire.pydantic.dev/) when a token is present, and
nowhere otherwise.

## In progress

- **Trunkline** — the web console above: usable, and still moving.
- **Per-user OAuth integrations** — GitHub and Linear, each user authorizing their
  own account from their own channel, so an agent acts as the person who asked.

## Anatomy

The codebase keeps an octopus metaphor, and these are the words it uses:

| Body part | Concept | What it is |
|---|---|---|
| **Octomate** 🐙 | `octomate/base.py` | The coordinator. Owns every tentacle, and the managers they share. |
| **Tentacle** 🦑 | `ChannelTentacle` | One per configured channel, keyed by instance: Slack, Lark, NapCat, Trunkline. |
| **Agent tentacle** 🧠 | `AgentTentacle` | One per agent: claude, codex, deepseek, inkling. |
| **Reflex** ⚡ | `octomate/reflex/` | The graph a signal runs through, from waking to a result or a suspension. |
| **Feeler** 🫧 | `feelers/` | The view. Decides how a streamed run event is rendered on a channel — timeline, segments and markdown, plus the cards you answer. |
| **Ink** 🖊️ | per-channel client | What actually sends, edits and uploads on the platform. |
| **Spill** 💧 | `SpillStore` | Where an oversized tool return goes, so it is read back on demand instead of re-sent every turn. |
| **Awake** 🌊 | `AwakeSignal` | What arrives: a message, or a batch of answered actions coming back. |
