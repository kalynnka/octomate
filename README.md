# Octomate 🐙

One durable record of every coding-agent session you run — including the ones you start
yourself, in your own terminal — and a place for you and your colleagues to continue
them: from Slack, Lark, QQ or the web, with whichever agent is worth the question.

> ⚠️ **Early development** (v0.0.1) — APIs and architecture are subject to change.

---

## It does not ask you to change how you work

Keep running `claude` or `codex` the way you already do: your terminal, your flags, your
harness, your choice of agent. There is no wrapper to launch through and no session to
start somewhere else first.

Octomate follows the transcript from a byte offset and takes the hook stream alongside
it, so every turn — the prompt, the tool calls, the answer, and any subagents it spawned
— lands in the same record as the work you drive from chat. Two commands set it up:

```bash
octomate claude hooks install
octomate codex hooks install
```

What that buys you is everything downstream of having the session at all: read it back
later, resume it from a chat thread, or hand the same context to a different agent
because the one you started with is not the one that should finish.

## One gateway, every channel

A run is an event stream that channels consume, rather than text one channel formats. The
same turn renders natively wherever it lands — streaming text, tool cards, todo lists,
approval buttons — and the thread it belongs to is the same thread on every surface.

| Channel | Transport | Inbound port |
|---|---|---|
| **Slack** | Slack Bolt, Socket Mode | not needed |
| **Lark / Feishu** | lark-oapi | webhook |
| **QQ** | NapCat, OneBot WebSocket | not needed |
| **Web** | dev UI, served by the same app | — |

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
  Slack / Lark / QQ / Web       a session you run yourself
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
                 inkling / claude / codex
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

## Built on pydantic-ai

Octomate is a pydantic-ai application. Agents, tools and capabilities are pydantic-ai's
own, the reflex graph is `pydantic-graph`, and an approval or a question is a pydantic-ai
deferred tool call — which is exactly why one survives a restart, since a deferred run is
a value that can be stored and handed back rather than a callback waiting in memory.
`pydantic-ai-harness` supplies the tool-output banding that keeps an oversized return
from being re-sent on every later turn, and persistence is SQLAlchemy behind Arcanus
transmuters.

## Agents

| Agent | Runtime | Models | Notes |
|---|---|---|---|
| **inkling** | native pydantic-ai agent | OpenAI, DeepSeek, Google, Anthropic, Bedrock | MCP toolsets, per-user integrations, oversized tool returns spilled rather than re-sent |
| **claude** | Claude Agent SDK | opus, sonnet, haiku | runs locally or over SSH on another host |
| **codex** | openai-codex SDK | gpt-5.5-codex | |

Claude and Codex both feed the native-session ingest above, so a session started in your
terminal and a run summoned from Slack are the same kind of thing afterwards.

Models are advertised through **claims** — what a route is for, and which thinking
efforts it accepts. A model with no claim is not summonable, so what an agent offers is
config rather than a hardcoded list.

## Quickstart

**Requirements:** Python 3.13+, [uv](https://docs.astral.sh/uv/).

The database is a SQLite file under `.octomate/`, so there is no infrastructure to stand
up first.

```bash
uv sync
uv run alembic upgrade head
cp octomate.default.yaml octomate.yaml   # add one provider key and one channel
uv run octomate serve
```

`octomate serve --reload` restarts on changes under `octomate/`. `octomate serve --tmux`
serves in a detached tmux session and attaches to it — creating it if it is not already
running, so the same command is both "start" and "go look at it". Octomate is meant to
outlive the terminal that started it: channels hold their sockets open, and the tailers
keep watching for native sessions started somewhere else entirely.

Docker is the other route, and it starts one container:

```bash
docker compose up -d
docker compose --profile qq up -d        # ...and a NapCat bridge, for QQ
```

## Configuration

Three layers, each overriding the one before:

| Layer | Where | Purpose |
|---|---|---|
| Defaults | `octomate.default.yaml` | Committed baseline; every key documented with its default |
| Overrides | `octomate.yaml` | Your credentials and deployment (gitignored) |
| Environment | `OCTOMATE__*` | Anything, at runtime |

Environment variables use `OCTOMATE__` with `__` as the nested delimiter, so
`OCTOMATE__CHANNELS__SLACK__BOT_TOKEN` sets `channels.slack.bot_token`.

The main sections are `agents` (inkling, claude, codex), `channels` (slack, lark, napcat,
dev_ui), `providers` (LLM credentials), `mcp` (vendor MCP servers on one operator token),
`integrations` (per-user OAuth) and `users`.

### Native session hooks

Configuring `agents.claude` or `agents.codex` serves a hook router (`/hooks/claude`, `/hooks/codex`) that native Claude Code / Codex sessions POST their prompts and answers into. Those routes write straight into thread history, which agents read back, so they authenticate — Octomate refuses to boot without a credential.

Set the credential up, then point your clients at it:

```bash
eval "$(octomate hooks secret)"                  # this shell
octomate hooks secret >> ~/.zshrc                # and every later one (zsh)
octomate claude hooks install                    # merges an http handler into ~/.claude/settings.json
octomate codex hooks install                     # merges a command handler into ~/.codex/hooks.json
```

`hooks secret` prints one line — `export OCTOMATE__HOOK_SECRET=…` — and writes nothing; where your login environment comes from is yours to know. Sessions only ever read the **environment**, and they are separate processes that never see your `octomate.yaml`, so that line is the bridge, and it has to reach whatever launches them.

`~/.zshrc` covers interactive zsh, which is what VSCode resolves the environment from; use `~/.zshenv` instead if you want non-interactive shells to have it too, and on another shell put the line wherever that shell would find it. Either way an environment is captured when a process starts: shells already open keep the one they had, and a GUI client (VSCode, the desktop app) grabs it when *it* launches — so restart them before expecting the hooks to carry the secret.

---

## Project structure

```
.
+-- main.py                    # Builds the FastAPI app - wires agents and channels
+-- octomate/
|   +-- base.py                # Octomate: the coordinator every tentacle is connected to
|   +-- reflex/                # The run graph - nodes, state, and the suspender
|   +-- tentacles/
|   |   +-- agents/             # inkling, claude, codex - adapters, ingest, tailers, hooks
|   |   `-- channels/           # slack, lark, napcat, web - and the feelers they draw with
|   +-- capabilities/          # Tools agents are given: gateway, ask, todos, history, harness
|   +-- managers/              # Threads, conversations, deferred actions, spills, users
|   +-- schemas/               # Pydantic/Arcanus transmuters - the persisted domain types
|   +-- models/                # SQLAlchemy ORM models behind those schemas
|   +-- config/                # Layered settings
|   +-- oauth/                 # Device and authorization-code flows, per user
|   `-- cli/                   # `octomate ...`
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

- **Octoview** — turn-by-turn review of agent changes in the editor, where the language
  server is already running. Its comments are meant to come back as an Octomate review
  batch that can be resumed into a run.
- **Per-user OAuth integrations** — GitHub and Linear, each colleague authorizing their
  own account from their own channel, so an agent acts as the person who asked.

## Anatomy

The codebase keeps an octopus metaphor, and these are the words it uses:

| Body part | Concept | What it is |
|---|---|---|
| **Octomate** 🐙 | `octomate/base.py` | The coordinator. Owns every tentacle, and the managers they share. |
| **Tentacle** 🦑 | `ChannelTentacle` | One per platform: Slack, Lark, NapCat, web. |
| **Agent tentacle** 🧠 | `AgentTentacle` | One per agent: inkling, claude, codex. |
| **Reflex** ⚡ | `octomate/reflex/` | The graph a signal runs through, from waking to a result or a suspension. |
| **Feeler** 🫧 | `feelers/` | The view. Decides how a streamed run event is rendered on a channel — timeline, segments and markdown, plus the cards you answer. |
| **Ink** 🖊️ | per-channel client | What actually sends, edits and uploads on the platform. |
| **Spill** 💧 | `SpillStore` | Where an oversized tool return goes, so it is read back on demand instead of re-sent every turn. |
| **Awake** 🌊 | `AwakeSignal` | What arrives: a message, or a batch of answered actions coming back. |
