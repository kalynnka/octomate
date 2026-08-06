# Trunkline — the Octomate web console

React + TypeScript + Vite implementation of the **Trunkline Console** design
(Claude Design project, Lonetrail design system). One screen, five panels:
threads sidebar · Control rail · review dossier · chat ledger · timeline,
over a status bar. See `DESIGN.md` for the visual world and `PRODUCT.md` for
product truth.

```bash
pnpm install
pnpm dev        # http://localhost:5173 — proxies /api and /oauth to 127.0.0.1:8000
pnpm build      # tsc -b && vite build
pnpm lint       # oxlint
```

The console runs fully on mock data when the Octomate server is down (the
status bar shows `relay offline`). With `octomate serve` running, the real
endpoints below take over automatically.

## Organization

```
web/
├── index.html                  # entry + design direction contract (comment)
├── public/fonts/               # vendored Lonetrail faces (Novecento, Plus Jakarta)
└── src/
    ├── main.tsx                # styles + root render
    ├── App.tsx                 # query client, theme bootstrap, initial thread
    ├── styles/
    │   ├── lonetrail.css       # DS tokens (verbatim): colors, type, shape, both themes
    │   ├── motion.css          # DS motion (verbatim): lt-* utilities, keyframes
    │   ├── decorators.css      # DS decorators (verbatim): gridpaper, dotband
    │   └── console.css         # console layer: trk-* tokens, rails, hover utilities
    ├── components/             # DS primitives: Icon, Button, BarChart, TriStripe,
    │   └── …                   #   Fold/Disclose, text style helpers
    ├── lib/
    │   ├── api/
    │   │   ├── types.ts        # domain types (mirrors octomate/schemas/*)
    │   │   ├── client.ts       # REAL endpoints: /api/health, /api/configure
    │   │   ├── index.ts        # api = real-where-available + mock elsewhere
    │   │   ├── hooks.ts        # TanStack Query hooks
    │   │   └── mock/           # design dataset + mock adapter (latency-simulated)
    │   └── useRailDrag.ts      # drag-to-resize for the four rails
    ├── state/
    │   └── console.ts          # zustand store: selection, panels, theme, ledger
    │                           #   overlays, feelers, review markup, mock turn scripts
    └── features/
        ├── shell/              # ConsoleShell (layout), StatusBar (live health)
        ├── threads/            # ThreadsSidebar: channel letter-rail + thread tree
        ├── chat/               # ChatMain, ChatHeader, Project/NewThread strips,
        │                       #   ChatLog + cards (tool/think/plan/feelers/files…),
        │                       #   Composer (assistant-ui), SessionBar, runtime.tsx
        ├── review/             # ReviewPanel: dossier tabs, diff/clean, line
        │                       #   quotes + comments that ride the next send
        ├── timeline/           # TimelinePanel: per-session event index
        └── control/            # ControlRail + ControlPage (Agents/MCP/Users/
                                #   Dashboard/Settings)
```

Architecture rules of thumb:

- **Domain shapes mirror the backend** (`Thread`, sessions ≈ `Conversation` +
  `Handoff`, feelers ≈ `DeferredAction`, projects ≈ `Project`). Swapping a
  mock for a real endpoint is a change in `lib/api`, never in features.
- **`@assistant-ui/react`** owns the chat runtime: the ledger's chat turns
  feed an external-store runtime (`features/chat/runtime.tsx`), the composer
  is `ComposerPrimitive`, and send flows through `onNew`. The ledger DOM
  itself is bespoke — it is an auditable ledger, not a bubble list. When the
  backend's Vercel-protocol `/api/chat` is wired, only `onNew`/the store's
  send path changes.
- **`state/console.ts`** is a faithful port of the design comp's logic class,
  including the scripted mock turns (relay send, review rev-folding, new
  thread boot). Those scripts are the seam where real streaming lands.

## Backend endpoints used today

| Endpoint | Use |
| --- | --- |
| `GET /api/health` | status-bar relay chip (offline/degraded/nominal), 15s poll |
| `GET /api/configure` | agent·model route list (falls back to mock routes) |
| `POST /api/chat` | *integration point* — Vercel AI data-stream chat; not yet wired into the mock threads |

The dev server proxies same-origin (`vite.config.ts`) because the backend
ships no CORS middleware; production should serve `dist/` from FastAPI.

## APIs the console needs that don't exist yet

Grounded in the current backend (`octomate/schemas/`, `octomate/managers/`):
the domain models exist, the HTTP surface doesn't.

1. **`GET /api/channels`** — channel tentacles with label/kind/health
   (`Octomate.channels`; sidebar channel rail, status-bar chips).
2. **`GET /api/threads`** — threads grouped by channel with derived title,
   status, active route (`ThreadManager` + `Thread.latest_handoff`; thread
   titles are not modeled today — needs a derived/stored title).
3. **`GET /api/threads/{id}`** — thread detail: `ThreadKey`, sessions
   (`Conversation`s + `Handoff` chain with kind entry/summon/teleport/ingest),
   project ref, usage/context totals (usage is not aggregated anywhere yet).
4. **`GET /api/threads/{id}/messages?before=`** — paged ledger with typed
   items (turns, thinking, tool calls + results, subagent runs, spills)
   rehydrated from `ThreadMessage` + `MessageBinding` + model messages
   (`ThreadManager.chat_messages_before`, `related_model_messages`).
5. **`GET /api/threads/{id}/events` (SSE/WebSocket)** — live ledger stream
   outside the request/response chat call, so approvals, native-agent
   ingests, and relayed turns land in an open console. Today the only stream
   is the per-request `/api/chat` SSE; there are no server WebSocket routes.
6. **`GET /api/deferred?thread=` + `POST /api/deferred/{batch_id}/respond`** —
   list pending feelers and answer them
   (`DeferredActionManager`, `DeferredActionBatchResponse` → `Octomate.kick`;
   powers the Approve/Dismiss and Ask cards from the console instead of IM).
7. **`POST /api/threads`** — create a web thread with route
   (agent/model/effort), project, branch; today a thread is only created
   implicitly by `/api/chat` with a fixed dev user.
8. **`GET /api/projects` / `POST /api/projects`** — registry list + register
   a new root (`ProjectManager.list`, YAML-declared today, so registration
   implies config write); plus **git state per project** (branch, dirty,
   ahead/behind) which no backend component provides yet.
9. **`GET /api/agents`** — agent tentacles with models, effort ranges, state,
   pool/hook info (`octomate/config/agents.py`, `config/models.py`; Agents
   page + composer route selector — `/api/configure` covers only route ids).
10. **`GET /api/mcp` · `GET /api/integrations` · `GET /api/connections`** —
    MCP servers with warm/cold status, OAuth connectors, per-user grants
    (`config/mcp.py`, `config/integrations.py`, `OAuthManager`; MCP page).
11. **`GET /api/users`** — users + linked channel profiles + grant state
    (`UserManager`; Users page).
12. **`GET /api/stats` · `GET /api/activity`** — dashboard tiles, gateway-verb
    counts, relay feed (no aggregation exists; needs counters or queries over
    threads/runs/handoffs).
13. **`GET /api/settings`** — provider/hook/observability snapshot of the
    resolved `OctomateConfig` (Settings page).
14. **`GET /api/spills/{handle}`** — read a spilled tool output
    (`SpillStore` exists with no HTTP surface; spill chips link to it).
15. **Dossier review** — file artifacts with revisions and line comments
    (review panel): no backend concept yet; closest seam is conversation
    artifacts + a `plan.apply_edits`-style tool contract.
16. **Relay verbs** — `POST /api/threads/{id}/teleport` and relay/send to
    another channel (reflex verbs exist in-process; not exposed over HTTP).
17. **Auth + CORS** — everything above needs a session story; today the web
    surface is a fixed `dev` user bound to 127.0.0.1 with no CORS headers.
