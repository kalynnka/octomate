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

The console is live-only for thread data: when the Octomate server is down
the status bar shows `relay offline` and the ledger surfaces stay empty
(only the shell's channel/control/status panels keep their mock
stand-ins). With the backend running it is an entry and a reader: every channel's threads and ledgers are read live from
`/api/trunkline`, directives create or continue threads on the trunkline
channel itself, and other channels' threads are read-only views. In production there is a single entry — `uvicorn
main:create_app --factory` serves this app's `dist/` at `/` alongside the API
(build with `pnpm build`); the Vite dev server is only for HMR.

## Organization

```
trunkline/
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
    │   │   ├── types.ts        # console domain types (render shapes)
    │   │   ├── events.ts       # wire types: 1:1 mirror of the trunkline SSE union
    │   │   ├── client.ts       # /api/trunkline endpoints + SSE reader
    │   │   ├── live.ts         # wire payloads → render shapes (threads, feelers)
    │   │   ├── index.ts        # api = live thread data + mock shell surfaces
    │   │   ├── hooks.ts        # TanStack Query hooks
    │   │   ├── fold.ts         # TurnFold: wire events → ledger cards (live + replay)
    │   │   └── mock/           # shell-surface stand-ins (channels, control, status)
    │   ├── queryClient.ts      # shared QueryClient (store invalidates after runs)
    │   └── useRailDrag.ts      # drag-to-resize for the four rails
    ├── state/
    │   └── console.ts          # zustand store: selection, panels, theme, ledger
    │                           #   overlays, feelers, live run driving
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
  is `ComposerPrimitive`, and send flows through `onNew` into the store's
  send path. The ledger DOM itself is bespoke — it is an auditable ledger,
  not a bubble list.
- **Live turns** stream over `POST /api/trunkline/threads/{key}/messages` as
  the backend's native event union (`lib/api/events.ts`), folded into ledger
  cards by `lib/api/fold.ts`. Thinking and tool cards live for the length of
  that stream: history reloads as the chat ledger alone, because the transcript
  they would be rebuilt from is the agent's own and never leaves the relay.
  Other channels' threads are read-only views of the same ledger.

## Backend endpoints used today (`/api/trunkline`)

The reads answer with the backend's own rows (`octomate/schemas/`), not a
console-shaped copy, and they nest the way the domain does — thread ›
conversation › run. `lib/api/events.ts` mirrors them field for field.

| Endpoint | Use |
| --- | --- |
| `GET  /health` | status-bar relay chip (offline/degraded/nominal), 15s poll |
| `GET  /routes` | new-thread agent·model picker; the pick rides only a thread's first directive (routes are fixed after that — re-routing awaits a manual handoff verb) |
| `GET  /permission-modes` | each registered agent's approval vocabulary, in the order ⇧⇥ steps through it — a provider's own scale, never a shared one — plus the configured default a conversation declaring nothing runs under. Absent agents cannot be switched: a tailed runtime's posture is read, not set |
| `GET  /threads` | every channel's threads (sidebar), newest first, each with its handoffs — without its messages |
| `GET  /threads/{id}` | one thread and its handoffs, by row id — any channel's |
| `GET  /threads/{id}/messages` | the chat ledger, oldest first; fetched when a thread is opened, never with the listing |
| `GET  /threads/{id}/conversations` | the thread's conversations, each carrying its runs — where each run ran (`cwd`), and which are a subagent's |
| `GET  /threads/{id}/project` | the project the work is in, or null; read-only and frozen on the thread |
| `GET  /threads/{id}/batches` | feelers still waiting, so a reload re-renders the cards a run is blocked on |
| `POST /threads/{id}/messages` | send a directive; SSE of the native run events — a fresh id creates the thread, an existing id continues it. `{id}` here is the platform thread key, not the row id the GETs take. Carries the posture picked for a thread that has no row yet |
| `PATCH /conversations/{id}/permission-mode` | switch a live conversation's approval posture; a run reads it as it starts, so the switch lands on the next turn |
| `POST /batches/{id}/resolve` | answer a feeler; SSE of the resumed run |

The SSE payloads are pydantic-ai's own `AgentStreamEvent` union plus
octomate's extension events, serialized natively (see
`octomate/capabilities/harness/events.py` (`WireEvent`) and its mirror
`src/lib/api/events.ts`). The dev server proxies same-origin
(`vite.config.ts`) because the backend ships no CORS middleware; production
serves `dist/` from the same FastAPI app.

## APIs the console needs that don't exist yet

Grounded in the current backend (`octomate/schemas/`, `octomate/managers/`):
the domain models exist, the HTTP surface doesn't. Now served by
`/api/trunkline`: thread list/detail with sessions and pending batches,
directive streaming with implicit thread create/continue, route list, and
feeler resolve (the old items 2, 3, 6, 7). Still missing:

1. **`GET /api/channels`** — channel tentacles with label/kind/health
   (`Octomate.channels`; sidebar channel rail and status-bar chips still use
   the static mock channel list).
2. **Server-paged ledger history** — `GET /threads/{id}/messages` answers with
   the whole ledger, and the console pages it client-side (latest page first,
   earlier on scroll-up). A `?before=` cursor is wanted once one thread's
   ledger outgrows one response.
3. **`GET /api/trunkline/events` (standing SSE/WebSocket)** — a live ledger
   stream outside the request/response run call, so native-agent ingests,
   IM-relayed turns, and other-session runs land in an open console. Today
   the only stream is per-directive.
4. **Thread create with a project** — a directive registers the thread but
   carries no workspace binding, so a console thread is never in a project.
   The new-thread strip says so rather than offering a picker: a thread's
   project is written with the thread, from the directory its session ran in,
   and is frozen after (`GET /threads/{id}/project` reads it).
5. **`GET /api/projects`** — the registry itself (`ProjectManager.projects`),
   for a reader that wants every known root rather than one thread's. No
   registration endpoint is wanted: a project is discovered from the native
   session found running in it. Plus **git state per project** (branch, dirty,
   ahead/behind), which no backend component provides — the project strip
   leaves the chip out until one does.
6. **`GET /api/agents`** — agent tentacles with models, effort ranges, state,
   pool/hook info (`octomate/config/agents.py`, `config/models.py`; Agents
   page — `/api/trunkline/routes` covers only route ids).
7. **`GET /api/mcp` · `GET /api/connections`** — MCP tentacles with warm/cold
    status, OAuth connectors, per-user grants (`config/mcp.py`, `OAuthManager`;
    MCP page).
8. **`GET /api/users`** — users + linked channel profiles + grant state
    (`UserManager`; Users page).
9. **`GET /api/stats` · `GET /api/activity`** — dashboard tiles, gateway-verb
    counts, relay feed (no aggregation exists; needs counters or queries over
    threads/runs/handoffs).
10. **`GET /api/settings`** — provider/hook/observability snapshot of the
    resolved `OctomateConfig` (Settings page).
11. **`GET /api/spills/{handle}`** — read a spilled tool output
    (`SpillStore` exists with no HTTP surface; spill chips link to it).
12. **Dossier review** — file artifacts with revisions and line comments
    (review panel): no backend concept yet; closest seam is conversation
    artifacts + a `plan.apply_edits`-style tool contract.
13. **Relay verbs** — `POST /api/threads/{id}/teleport` and relay/send to
    another channel (reflex verbs exist in-process; not exposed over HTTP).
14. **Auth + CORS** — everything above needs a session story; today the web
    surface is a fixed `dev` user bound to 127.0.0.1 with no CORS headers.
15. **A title on the thread row** — the sidebar names each thread by its
    surface (the platform thread key), because a listing carries no messages
    and reading one line per thread would be a request per row. The fix is a
    column on the thread, written from its first human message.
