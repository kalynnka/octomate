# Octomate 🐙

An octopus-themed, multi-platform AI assistant. Octomate connects to messaging platforms through **tentacles**, routes messages through its central **nerve**, and responds using a fleet of LLM agents. Think of it as an octopus: one brain, many arms, each arm reaching a different chat platform.

> ⚠️ **Early development** (v0.0.1) — APIs and architecture are subject to change.

---

## How It Works

```
User sends a message on Slack / Lark / QQ
         │
         ▼
  ChannelTentacle          ← platform adapter (Slack, Lark, NapCat)
  buffers + batches it
         │  nerve (anyio stream)
         ▼
      Octopus 🐙           ← central coordinator, routes by thread ownership
         │
         ▼
   Flick Agent ⚡          ← per-tentacle quick brain (Google Gemini)
   ┌──────┴──────┐
ANSWER        SUMMON              SILENT
   │              │                  │
respond       hand off          ignore it
directly   AgentTentacle
           (Claude Code)
```

1. A **ChannelTentacle** listens for incoming messages, buffers them briefly (configurable flush delay), and pushes them through the **Nerve**.
2. The **Octopus** coordinator dispatches the batch to the owning tentacle.
3. The tentacle's **Flick** agent decides: **ANSWER** directly, **SUMMON** a specialist `AgentTentacle`, or stay **SILENT**.
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
| **Flick** ⚡ | `create_flick_agent()` (`octomate/agents/flick.py`) | Per-tentacle quick brain — a pydantic-ai Agent on Google Gemini. First to touch every message. |
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

- **Claude Code** (`octomate/tentacles/claude.py`) — wraps the Claude Agent SDK for coding, file editing, and shell commands. Summoned by Flick when a complex task arrives. Streams responses back to the chat with thinking blocks, live progress, and approval cards.

---

## Skills (`octotools/`)

Pluggable toolsets that Flick (and other agents) can call:

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
│   │   ├── flick.py           # ⚡ Per-tentacle quick agent (Gemini)
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
- **pydantic-ai** — agent framework (Google Gemini for Flick by default)
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
