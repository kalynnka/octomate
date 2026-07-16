# Octomate 🐙

An octopus-themed, multi-platform AI assistant. Octomate connects to messaging platforms through **tentacles**, routes messages through its central **nerve**, and responds using a fleet of LLM agents. Think of it as an octopus: one brain, many arms, each arm reaching a different chat platform.

> ⚠️ **Early development** (v0.0.1) — APIs and architecture are subject to change.

---

```
Deep in the digital sea it stirs,
one brain, eight arms, a thousand words.
A message drifts from Slack or Lark —
a tentacle catches it, quick in the dark.

The nerve hums low, a current of thought,
routing each message the users have brought.
Pulse fires first — a flash, a spark —
ANSWER or SUMMON or vanish, remarked.

When summoned, Claude rises from the deep,
editing code while the mortals sleep.
Feelers reach out with a card to approve,
ink flows back in a smooth, steady groove.

Memory lingers like brine in the tide,
recalling the threads that once swept inside.
Skills slot and unslot like arms finding grip —
GitHub, Linear, a tarot card flip.

One octopus, calm at the center of all,
orchestrating the current, awaiting the call.
Eight platforms, one mind, no tangle, no fuss —
that's the quiet art of Octomate. 🐙

                                        — Claude
```

---

## How It Works

```
User sends a message on Slack / Lark / QQ
         │
         ▼
  ChannelTentacle          ← platform adapter (Slack, Lark, NapCat)
  dispatches it
         │  nerve (anyio stream)
         ▼
      Octopus 🐙           ← central coordinator, routes by thread ownership
         │
         ▼
   Pulse Agent ⚡          ← per-tentacle quick brain (Google Gemini)
   ┌──────┴──────┐
ANSWER        SUMMON              SILENT
   │              │                  │
respond       hand off          ignore it
directly   AgentTentacle
           (Claude Code)
```

1. A **ChannelTentacle** listens for incoming messages and pushes each event through the **Nerve**.
2. The **Octopus** coordinator dispatches the event to the owning tentacle.
3. The tentacle's **Pulse** agent decides: **ANSWER** directly, **SUMMON** a specialist `AgentTentacle`, or stay **SILENT**.
4. If a tool requires human approval, **Feelers** send interactive cards (approve / deny buttons) inside the chat platform.
5. **Memory** is recalled before each interaction and updated after.

---

## Anatomy

Octomate uses an octopus anatomy metaphor throughout the codebase:

| Body Part | Concept | Description |
|---|---|---|
| **Octopus** 🐙 | `Octopus` (`octomate/octopus.py`) | Central coordinator. Owns all tentacles and routes messages through the nerve. |
| **Tentacle** 🦑 | `ChannelTentacle` (`octomate/tentacles/base.py`) | Platform adapter. One per IM platform: `SlackTentacle`, `LarkTentacle`, `NapcatTentacle`. |
| **Agent Tentacle** 🧠 | `AgentTentacle` (`octomate/tentacles/base.py`) | Specialist agent (e.g. `ClaudeCodeTentacle`) summoned for complex tasks. |
| **Pulse** ⚡ | `create_pulse_agents()` (`octomate/agents/pulse.py`) | Per-tentacle quick brain — a pydantic-ai Agent on Google Gemini. First to touch every message. |
| **Nerve** 🔗 | anyio object stream inside `Octopus` | Message bus that connects tentacles to the coordinator. |
| **Feeler** 🫧 | `Feelers` (`octomate/tentacles/feelers.py`) | Interactive UI: tool-call confirmations, user questions, todo cards. Platform-specific for Slack and Lark. |
| **Ink** 🖊️ | Platform API client per tentacle | Sends messages, uploads files, updates cards. |
| **Skill** 🛠️ | `SkillManager` (`octomate/agents/manager.py`) | Pluggable tool system. Skills are function sets or MCP servers loaded/unloaded at runtime. |
| **Memory** 💭 | `OctopusMemory` / `Mem0Memory` / `ZepMemory` (`octomate/memory/`) | Conversation recall. Supports basic in-memory, Mem0 (Milvus vector DB), or Zep Cloud backends. |

---

## Supported Platforms

| Platform | Tentacle | Transport |
|---|---|---|
| **Slack** | `SlackTentacle` | Slack Bolt, Socket Mode |
| **Lark / Feishu** | `LarkTentacle` | lark-oapi SDK, webhook |
| **QQ** | `NapcatTentacle` | NapCat, OneBot WebSocket |

---

## Agent Tentacles

- **Claude Code** (`octomate/tentacles/claude.py`) — wraps the Claude Agent SDK for coding, file editing, and shell commands. Summoned by Pulse when a complex task arrives. Streams responses back to the chat with thinking blocks, live progress, and approval cards.

---

## Skills (`octotools/`)

Pluggable toolsets that Pulse (and other agents) can call:

| Skill | File | Description |
|---|---|---|
| Streamify | `streamify.py` | Streamify platform integration |
| GitHub | `github.py` | GitHub operations (approval-gated write actions) |
| Linear | `linear.py` | Linear issue tracker |
| Pixiv | `pixiv.py` | Artwork search |
| QWeather | `qweather.py` | Weather queries |
| Tarot 🔮 | `tarot/` | Tarot card reading |

---

## Project Structure

```
.
├── main.py                    # Entry point — wires tentacles, starts uvicorn
├── octomate/                  # Core library
│   ├── octopus.py             # 🐙 Central coordinator
│   ├── config.py              # Configuration (pydantic-settings, YAML + env)
│   ├── database.py            # SQLAlchemy async setup (SQLite by default)
│   ├── agents/
│   │   ├── pulse.py           # ⚡ Per-tentacle pulse agent (Gemini)
│   │   ├── manager.py         # Skill discovery and management
│   │   ├── prompts.py         # System prompts
│   │   └── tools.py           # Built-in tools (chat history)
│   ├── tentacles/
│   │   ├── base.py            # Abstract ChannelTentacle & AgentTentacle
│   │   ├── claude.py          # Claude Code agent tentacle
│   │   ├── feelers.py         # Interactive UI (confirmations, questions, todos)
│   │   ├── slack/             # Slack tentacle
│   │   ├── lark/              # Lark/Feishu tentacle
│   │   └── napcat/            # QQ (NapCat/OneBot) tentacle
│   ├── memory/                # Memory backends (Mem0, Zep, basic)
│   ├── schemas/               # Pydantic models (events, actions, sessions)
│   ├── stores/                # Persistence (threads, messages, interactions)
│   ├── models/                # SQLAlchemy ORM models
│   └── transmuters/           # Message format converters
├── octotools/                 # 🛠️ Pluggable skills
├── migrations/                # Alembic database migrations
├── docs/                      # Documentation
├── tests/                     # Test suite
├── octomate.default.yaml      # Default config template
├── docker-compose.yml         # Dev environment (app + NapCat + Milvus + Postgres + pgAdmin)
└── pyproject.toml             # Project metadata & dependencies
```

---

## Tech Stack

- **Python 3.13** · **[uv](https://docs.astral.sh/uv/)** package manager
- **pydantic-ai** — agent framework (Google Gemini for Pulse by default)
- **claude-agent-sdk** — Claude Code agent tentacle
- **pydantic / pydantic-settings** — config and schema validation
- **SQLAlchemy** (async + aiosqlite) · **Alembic** — database and migrations
- **anyio** — async primitives and the nerve stream
- **httpx** — HTTP client with retry transport
- **Mem0 + Milvus** — vector-based long-term memory (optional)
- **Zep Cloud** — alternative memory backend (optional)
- **slack-bolt** — Slack bot framework
- **lark-oapi** — Lark/Feishu SDK
- **websockets** — WebSocket client for NapCat/QQ
- **uvicorn** — ASGI server (Lark webhook handling)

---

## Development Setup

**Requirements:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker & Docker Compose.

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp octomate.default.yaml octomate.yaml
# Edit octomate.yaml — add your Gemini API key, Slack tokens, Lark credentials, etc.

# 3. Start infrastructure (Milvus, Postgres, pgAdmin, optionally NapCat)
docker compose up -d

# 4. Run database migrations
uv run alembic upgrade head

# 5. Start the app
uv run uvicorn main:app --reload --reload-dir octomate --reload-dir octotools

# 6. Run tests
uv run pytest
```

---

## Configuration

Octomate uses a layered config system via **pydantic-settings**:

| Layer | File | Purpose |
|---|---|---|
| Defaults | `octomate.default.yaml` | Committed baseline |
| Overrides | `octomate.yaml` | Your local credentials (gitignored) |
| Env vars | `OCTOMATE_*` | Override any value at runtime |

Key config sections:
- `octomate.tentacles[]` — platform connections (Slack, Lark, NapCat)
- `octomate.agents[]` — agent tentacles (e.g. Claude Code)
- Per-skill sections: `github`, `linear`, `streamify`, `pixiv`, `qweather`, `tarot`

Environment variables use the prefix `OCTOMATE__` with `__` as the nested delimiter — e.g. `OCTOMATE__GEMINI__API_KEY` overrides `gemini.api_key` in YAML.

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

Octomate itself reads the secret from `octomate.yaml` (`octomate.hook_secret`), `.env` (`OCTOMATE__HOOK_SECRET=…`), or the environment — both files are gitignored, and a set environment variable wins over `octomate.yaml`, which wins over `.env`. An already-configured secret is printed as-is and never rotated. If none is configured, `hooks secret` makes one and prints the line anyway, and tells you on stderr to give it to Octomate too — until you do, the routers will refuse the hooks.

Both installers write a *reference* to the variable, never its value. Claude does this with `headers` + `allowedEnvVars`; Codex has no per-hook `env`, and a secret on its command line would be world-readable in `ps`, so it reads the variable in the hook itself. That also keeps hook config files safe to commit and share.

The transport is HTTP on both sides — Codex has no http hook handler, so its command hook is a small stdlib-only script that POSTs to the same router. Because it authenticates rather than trusting reachability, Octomate does not have to be on the same machine as the sessions.

A hook names the transcript it wants tailed, and Octomate only follows paths inside a known transcript tree — `<CLAUDE_CONFIG_DIR or ~/.claude>/projects` and `<CODEX_HOME or ~/.codex>/sessions`. Those are where the clients write today rather than a promise they always will, so `agents.claude.transcript_root` / `agents.codex.transcript_root` add another. They widen the set rather than replacing it: the client's own tree stays accepted, so adding a root can never be why a session stops being ingested. If a session's turns reach the ledger but its tools and reasoning never do, look for a refused transcript path in the logs — that setting is the fix.
