# Plan: GitHub + Linear MCP skills (shared token, progressive loading)

> **Status:** proposed · **Owner:** @luhui · **Created:** 2026-06-16
> **Supersedes:** `.github/prompts/plan-gitHubMcp.prompt.md` (the old Docker-stdio /
> `SkillManager` / `octotools/` sketch — stale vs. the current tree, and heavier
> than needed). · **Defers:** per-user OAuth + the central identity/profile system
> (parked; see chat history). This ships the *tools* on a **single shared
> operator token**, not per-user delegation.

## TL;DR

Give the inkling agent GitHub and Linear tools by connecting to each vendor's
**official hosted MCP server** over Streamable HTTP, authenticated with **one
operator-supplied token per MCP server** passed in the `Authorization` header.
No Docker, no npx, no subprocess, no callback server, no OAuth.

Keep the model's tool list small with pydantic-ai's **built-in deferred
loading**: call `.defer_loading()` on each MCP toolset and the auto-injected
`ToolSearch` capability hides those tools until the model discovers them by
intent. No custom `load_skill`/`unload_skill` machinery.

## Why hosted + shared token

- **GitHub** hosted MCP (`https://api.githubcopilot.com/mcp/`) speaks Streamable
  HTTP and accepts a PAT via `Authorization: Bearer <PAT>`; it also auto-hides
  tools the token lacks scope for.
- **Linear** hosted MCP (`https://mcp.linear.app/mcp`) accepts a **personal API
  key** (`lin_api_…`) via `Authorization: Bearer <key>` as an alternative to
  interactive OAuth. (A raw, non-`Bearer` key is rejected with 401.)

Both data sets already live on those platforms, so a hosted connection exposes
nothing new. This honors "use my token, postpone OAuth" with the smallest
surface. Per-user token selection is a later, additive change (route the header
off `InklingDeps` once the identity layer exists) — explicitly out of scope here.

## Progressive loading — the built-in mechanism (pydantic-ai 1.107)

- `toolset.defer_loading()` → returns a `DeferredLoadingToolset` that marks every
  tool `defer_loading=True`, hiding it from the model until discovered.
- The `ToolSearch` capability is **auto-injected into every agent** (zero overhead
  when nothing is deferred). On Anthropic/OpenAI the provider drives discovery
  natively; on the configured **Gemini/Vertex** model it falls back to a local
  `search_tools` function the model calls. Verified: the deferred corpus rides in
  `function_tools` and `Model.prepare_request` does the wire-level hiding.

Net: build server → `.defer_loading()` → append to `toolsets`. Nothing else.

## Design

### 1. Dependency bump (done)

`pydantic-ai>=1.107.0` (+ `pydantic-ai-slim`). Synced; full suite green
(233 passed, 11 skipped). `pydantic_ai._agent_graph.build_run_context` (private,
used by [capabilities/agent.py](../../octomate/capabilities/agent.py)) still
resolves. Deprecation warnings noted as a separate follow-up, not handled here:
`AgentBuiltinTool`→`AgentNativeTool`, `agent.run(builtin_tools=/output_retries=)`,
`pydantic_graph.Graph` (v2 removal), redundant `GoogleProvider(vertexai=False)`.

### 2. Config — a new `mcp` section

New `octomate/config/mcp.py`:

```python
class GitHubMcpConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://api.githubcopilot.com/mcp/"
    read_only: bool = False  # selects the /readonly endpoint variant

class LinearMcpConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://mcp.linear.app/mcp"

class McpConfig(BaseModel):
    github: GitHubMcpConfig | None = None
    linear: LinearMcpConfig | None = None
```

Wire `mcp: McpConfig` onto `OctomateConfig`
([config/__init__.py](../../octomate/config/__init__.py)) and export the new
models. Env follows the existing nested pattern:
`OCTOMATE__MCP__GITHUB__TOKEN`, `OCTOMATE__MCP__LINEAR__TOKEN`.
Add commented stubs to `octomate.default.yaml`.

### 3. Toolset builder

New `octomate/tentacles/agent/inkling/mcp.py`:

```python
def build_mcp_toolsets(
    config: McpConfig,
) -> list[AbstractToolset[None]]:
    toolsets = []
    if (gh := config.github) and gh.enabled:
        if gh.token is None:
            raise ValueError("mcp.github.enabled but no token set")
        toolsets.append(
            MCPToolset(
                gh.url,
                headers={"Authorization": f"Bearer {gh.token.get_secret_value()}"},
                id="github",
            ).defer_loading()
        )
    if (lin := config.linear) and lin.enabled:
        if lin.token is None:
            raise ValueError("mcp.linear.enabled but no token set")
        toolsets.append(
            MCPToolset(
                lin.url,
                headers={"Authorization": f"Bearer {lin.token.get_secret_value()}"},
                id="linear",
            ).defer_loading()
        )
    return toolsets
```

Fail-fast on enabled-without-token (per AGENTS.md: no silent fallback).

### 4. Wire into the agent

In [main.py](../../main.py), extend the agent's `toolsets`:

```python
toolsets=[inkling_toolset, *build_mcp_toolsets(config.mcp)],
```

`ToolSearch` is auto-injected — no capability change. The static
`inkling_toolset` (e.g. `ask_questions`) stays eagerly loaded; only the MCP
tools defer.

### 5. Connection lifecycle — keep MCP warm via the tentacle context protocol

The react graph calls `agent.run()` per node. Passing MCP servers as toolsets
makes pydantic-ai enter/exit them per run unless the agent is already entered,
which would reconnect + re-`initialize` the remote servers on every turn. The
warm session must therefore outlive a single run and span the process lifetime.

**Make every tentacle an async context manager.** `Tentacle`
([tentacles/base.py](../../octomate/tentacles/base.py)) now defines
`__aenter__`/`__aexit__` (no-op defaults) in place of the old
`activate()`/`deactivate()` pair, and the host just enters every tentacle. Each
tentacle owns its own long-lived resources: agents hold warm MCP sessions,
channels hold the inbound receive loop. This keeps `base.py`'s lifespan free of
pydantic-ai *and* per-platform specifics.

The `InklingTentacle` ([inkling/base.py](../../octomate/tentacles/agent/inkling/base.py))
enters/exits its wrapped pydantic-ai agent via an `AsyncExitStack` it owns:

```python
async def __aenter__(self) -> Self:
    # Entering the agent opens + `initialize`s every toolset's MCP session once;
    # they stay warm until __aexit__.
    await self._exit_stack.enter_async_context(self.agent)
    return self

async def __aexit__(self, *exc: object) -> None:
    await self._exit_stack.aclose()
```

`cache_tools=True` (default) means the deferred corpus is fetched once and reused
across runs. With no MCP toolsets configured the agent has nothing to open, so
entering is a cheap no-op — agents that never declare MCP servers pay nothing.

**Channels** do their setup/teardown directly in `__aenter__`/`__aexit__`. The
`ChannelTentacle` base `__aenter__` resolves identity (`probe()`); each platform
channel overrides it (calling `super().__aenter__()` first) to open its client and
overrides `__aexit__` to close it. Most channels need no background task — Slack's
SDK self-drives the socket, and Lark stays alive via its `_connect` + ping task —
so their old `stop_event`/parking loop is gone. Only Napcat actively reads off the
socket, so it spawns its reconnect loop as a background task in `__aenter__` and
cancels/joins it in `__aexit__`. HTTP-driven channels (Vercel) inherit the base
probe-only `__aenter__`.

**Host wiring.** The lifespan in [base.py](../../octomate/base.py) is now just an
`AsyncExitStack` entering every tentacle — agents first (warm tools before any
channel ingests), then channels; teardown unwinds in reverse:

```python
async with AsyncExitStack() as stack:
    for agent in self.agents.values():
        await stack.enter_async_context(agent)
    for channel in self.channels.values():
        await stack.enter_async_context(channel)
    yield
```

### 6. `octomate.default.yaml`

```yaml
# mcp:
#   github:
#     enabled: true
#     token: ghp_xxx            # or OCTOMATE__MCP__GITHUB__TOKEN
#   linear:
#     enabled: true
#     token: lin_api_xxx        # or OCTOMATE__MCP__LINEAR__TOKEN
```

## Decisions settled

- **Hosted, not self-hosted.** Remote Streamable HTTP for both; no Docker/npx
  lifecycle. (Local Docker stdio remains a future option behind `url`.)
- **Shared operator token, not per-user.** One PAT / one Linear key. Per-user
  delegation is deferred with the identity work.
- **Built-in `defer_loading()` + `ToolSearch`, not a custom skill gate.** The old
  plan's `load_skill`/`unload_skill` meta-tools are unnecessary in 1.107.
- **`mcp` config section**, parallel to `channels`/`providers`.
- **MCP lifecycle owned by the agent tentacle** via the async-context protocol
  (`__aenter__`/`__aexit__`), not the FastAPI lifespan. The host just enters every
  tentacle; the tentacle warms its sessions.
- **Per-vendor tool-name prefixes** (`github_…`, `linear_…`) via `.prefixed()`.
  Required, not optional: both servers expose `list_issues`, which pydantic-ai
  rejects as a collision when combined. Applied before `.defer_loading()`.

## Verification

1. `uv run pytest` stays green (already true post-bump).
2. Config: `mcp.github.enabled=true` with token parses; enabled without
   token raises at builder time.
3. Builder: returns a deferred toolset only for enabled servers; disabled →
   empty list.
4. Unit (TestModel/FunctionModel): with a deferred toolset wired, a run can call
   the injected `search_tools`, then a discovered tool resolves through the react
   stack. (FunctionModel reports the pre-filter corpus, so assert on the
   discovery round-trip, not the raw `info.function_tools` list.)
5. Live smoke (manual, real token): app boots, MCP sessions connect once; in chat
   "list my GitHub issues" / "show my Linear issues" triggers tool search →
   discovered tool call → result. Confirm tools are absent from the model's
   initial tool list until searched.
6. Lifecycle: entering the `InklingTentacle` (`async with`) opens the MCP
   sessions once and exiting closes them cleanly (assert the host lifespan
   enters every tentacle, and that a disabled-MCP agent enters as a no-op).
   Napcat's `__aenter__` spawns its reconnect loop as a background task that
   `__aexit__` cancels and joins.

## Future thinking — dynamic MCP install (NOT this run's scope)

This run hard-codes two typed servers (`github`, `linear`) behind static config.
The question worth parking: **how could a user/admin add or install MCP servers at
runtime without a code change?** Sketching the option space, cheapest first — the
agent-tentacle lifecycle from §5 is what makes the harder options tractable,
because "warm a server" and "tear one down" are already first-class operations the
tentacle owns.

- **A. Generic server map in config (smallest real step).** Replace the two typed
  fields with `servers: dict[str, McpServerConfig]`, where `McpServerConfig` is a
  discriminated union on transport (`streamable_http` remote vs `stdio` local
  Docker/npx). The two named servers become two entries. Adding a server = editing
  yaml/env + restart; no code. `github`/`linear` keep convenience subclasses with
  baked-in `url` defaults. Honest limit: still restart-based, still operator-edited
  files. Good migration target even within the static design.

- **B. Persisted registry + admin API (true runtime add).** Store `McpServerConfig`
  rows via Arcanus (schema/transmuter, with the secret as a reference into a
  secrets store, never plaintext). An admin-only FastAPI router (owned by the
  Octomate instance per the architecture rules, not a channel) does
  CRUD. On create: build the toolset → `.defer_loading()` → `enter_async_context`
  it into the *live* agent tentacle's `AsyncExitStack` → it's immediately
  discoverable via `ToolSearch`, no restart. On delete: pop/close that one stack
  entry. The hot-attach hinges on §5's design: the tentacle already owns the stack
  and the warm-session contract, so "add server N+1" is one more
  `enter_async_context`. The real work is a *mutable composite toolset* the agent
  reads through, so the model picks up additions without rebuilding the `Agent`.

- **C. Catalog / marketplace install (B + identity).** Admin browses a curated
  catalog (à la Claude connectors), picks a server, supplies a token or runs OAuth,
  spec is stored encrypted and attached as in B. This is where the parked per-user
  OAuth + identity/profile work re-enters: per-user installs mean the
  `Authorization` header is resolved per run off `InklingDeps`/identity, and the
  warm-session model from §5 may need per-principal session pools rather than one
  process-wide session. Largest surface; depends on the identity layer landing
  first.

- **D. Per-run / per-conversation toolsets (dynamic but cold).** `run()` already
  accepts `toolsets=`, so a server could be attached for a single conversation
  without touching the warm set. Cheap to reason about, but reconnects +
  `initialize`s per run (the exact cost §5 avoids) — fine for one-off/scoped use,
  wrong for anything hot-path.

Cross-cutting concerns any of B–D must answer before they're safe:

- **Trust & isolation.** Arbitrary MCP install is an SSRF / prompt-injection /
  tool-shadowing vector (a malicious server can mimic a trusted tool name).
  Minimum: admin-gated installs, a URL/host allowlist, and collision detection on
  tool names surfaced through `ToolSearch` (ties into the prefix/rename item below).
- **Secret handling.** Tokens must live in a secrets store with references in the
  DB, scrubbed from logs/logfire the same way channel creds are today.
- **Failure modes.** A dead/slow server must fail its own `activate()` without
  taking down the agent or the host — per-server try/scope, not one shared stack
  that aborts on the first bad entry.

Recommendation when this is picked up: **A now-ish** (mechanical, low risk, sets the
data shape), then **B** as the first real "install at runtime" milestone, with **C**
folded into the identity epic.

## Out of scope (later)

- Per-user tokens / OAuth / central identity + profile system (parked).
- Read-only enforcement beyond GitHub's `read_only` endpoint variant.
- Deprecation cleanup from the 1.107 bump.
- Additional skills (the README's Pixiv/QWeather/Tarot etc.).
