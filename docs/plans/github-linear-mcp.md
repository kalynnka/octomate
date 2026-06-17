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
operator-supplied token per integration** passed in the `Authorization` header.
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
  key** in the `Authorization` header as an alternative to interactive OAuth.

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

### 2. Config — a new `integrations` section

New `octomate/config/integrations.py`:

```python
class GitHubIntegrationConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://api.githubcopilot.com/mcp/"
    read_only: bool = False  # selects the /readonly endpoint variant

class LinearIntegrationConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://mcp.linear.app/mcp"

class IntegrationsConfig(BaseModel):
    github: GitHubIntegrationConfig | None = None
    linear: LinearIntegrationConfig | None = None
```

Wire `integrations: IntegrationsConfig` onto `OctomateConfig`
([config/__init__.py](../../octomate/config/__init__.py)) and export the new
models. Env follows the existing nested pattern:
`OCTOMATE__INTEGRATIONS__GITHUB__TOKEN`, `OCTOMATE__INTEGRATIONS__LINEAR__TOKEN`.
Add commented stubs to `octomate.default.yaml`.

### 3. Toolset builder

New `octomate/tentacles/agent/inkling/integrations.py`:

```python
def build_integration_toolsets(
    config: IntegrationsConfig,
) -> list[AbstractToolset[None]]:
    toolsets = []
    if (gh := config.github) and gh.enabled:
        if gh.token is None:
            raise ValueError("integrations.github.enabled but no token set")
        toolsets.append(
            MCPServerStreamableHTTP(
                gh.url,
                headers={"Authorization": f"Bearer {gh.token.get_secret_value()}"},
                id="github",
            ).defer_loading()
        )
    if (lin := config.linear) and lin.enabled:
        if lin.token is None:
            raise ValueError("integrations.linear.enabled but no token set")
        toolsets.append(
            MCPServerStreamableHTTP(
                lin.url,
                headers={"Authorization": lin.token.get_secret_value()},
                id="linear",
            ).defer_loading()
        )
    return toolsets
```

Fail-fast on enabled-without-token (per AGENTS.md: no silent fallback).

### 4. Wire into the agent

In [main.py](../../main.py), extend the agent's `toolsets`:

```python
toolsets=[inkling_toolset, *build_integration_toolsets(config.integrations)],
```

`ToolSearch` is auto-injected — no capability change. The static
`inkling_toolset` (e.g. `ask_questions`) stays eagerly loaded; only the MCP
tools defer.

### 5. Connection lifecycle — keep MCP warm

The react graph calls `agent.run()` per node. Passing MCP servers as toolsets
makes pydantic-ai enter/exit them per run unless the agent is already entered,
which would reconnect + re-`initialize` the remote servers on every turn.

Enter the inkling agent once for the app's lifetime in the FastAPI lifespan
([base.py:91](../../octomate/base.py#L91)): `async with agent:` (via
`AsyncExitStack`) around the existing channel-activation block, so the warm MCP
sessions live for the process and tear down on shutdown. The agent handle is
reachable from the registered `InklingTentacle`. `cache_tools=True` (default)
means the deferred corpus is fetched once and reused.

### 6. `octomate.default.yaml`

```yaml
# integrations:
#   github:
#     enabled: true
#     token: ghp_xxx            # or OCTOMATE__INTEGRATIONS__GITHUB__TOKEN
#   linear:
#     enabled: true
#     token: lin_api_xxx        # or OCTOMATE__INTEGRATIONS__LINEAR__TOKEN
```

## Decisions settled

- **Hosted, not self-hosted.** Remote Streamable HTTP for both; no Docker/npx
  lifecycle. (Local Docker stdio remains a future option behind `url`.)
- **Shared operator token, not per-user.** One PAT / one Linear key. Per-user
  delegation is deferred with the identity work.
- **Built-in `defer_loading()` + `ToolSearch`, not a custom skill gate.** The old
  plan's `load_skill`/`unload_skill` meta-tools are unnecessary in 1.107.
- **`integrations` config section**, parallel to `channels`/`providers`.

## Verification

1. `uv run pytest` stays green (already true post-bump).
2. Config: `integrations.github.enabled=true` with token parses; enabled without
   token raises at builder time.
3. Builder: returns a deferred toolset only for enabled integrations; disabled →
   empty list.
4. Unit (TestModel/FunctionModel): with a deferred toolset wired, a run can call
   the injected `search_tools`, then a discovered tool resolves through the react
   stack. (FunctionModel reports the pre-filter corpus, so assert on the
   discovery round-trip, not the raw `info.function_tools` list.)
5. Live smoke (manual, real token): app boots, MCP sessions connect once; in chat
   "list my GitHub issues" / "show my Linear issues" triggers tool search →
   discovered tool call → result. Confirm tools are absent from the model's
   initial tool list until searched.
6. Shutdown: MCP sessions close cleanly with the lifespan.

## Out of scope (later)

- Per-user tokens / OAuth / central identity + profile system (parked).
- Read-only enforcement beyond GitHub's `read_only` endpoint variant.
- Tool-prefix/rename to disambiguate if GitHub and Linear ever collide on a name
  (none known today; revisit if `ToolSearch` surfaces a clash).
- Deprecation cleanup from the 1.107 bump.
- Additional skills (the README's Pixiv/QWeather/Tarot etc.).
