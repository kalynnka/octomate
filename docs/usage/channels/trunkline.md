# Trunkline

Trunkline is Octomate's own web console, and the one channel that is not somebody
else's chat app. It is both an entry and a reader: threads on the `trunkline`
channel are yours to start and continue from the browser, and every other channel's
threads, native sessions included, are readable there. It is also where you
register, issue tokens, link profiles, install MCP connectors and switch a
conversation's permission mode.

!!! note "Preview"
    Usable, and changing week to week. Panels, API shape and design are all still
    moving.

## Configure

```yaml
tentacles:
  trunkline:
    type: trunkline
    agents: [claude, inkling]
    static_dir: /srv/octomate/app/trunkline/dist   # omit for the API alone
```

`agents` sets the entry order; every registered agent's models are available in the
console regardless. `mention_only` never applies: there is no group to be mentioned
in. Streaming is on and every run event is forwarded as it happens.

Sign-in needs an `auth:` block. The console API is at `/api/trunkline`, cookie
authenticated, with `X-Octomate-Request: 1` on writes, so the console and the API
must share an origin.

Name agents you have already enabled, check the configuration and restart Octomate
after the build below. [Create your account](../../installation/accounts.md),
sign in and start a conversation to verify a reply. The
[macOS walkthrough](../../installation/macos.md) includes this setup end to end.

## Build or develop

```sh
cd trunkline
pnpm install --frozen-lockfile
pnpm build      # dist/, the directory static_dir points at
pnpm dev        # http://localhost:5173, proxying /api and /oauth to 127.0.0.1:8000
```

The server ships no CORS middleware, which is why the dev server proxies. Use
`octomate service serve --reload` for backend development. A first-serve mount of
`static_dir` at `/` is what makes the built console the front page.

## What you can do there

- **Start a thread** with a message, picking an agent and model, and optionally a
  project on the first message. A thread keeps its agent and model; a different
  pick on an owned thread is refused, because a mid-thread model switch busts the
  provider's cache. Hand off explicitly instead.
- **Read any thread**: the chat ledger, each agent conversation and its runs, the
  handoffs, and the pending actions.
- **Answer approvals and questions**, for any channel's thread. A batch already
  resolved is refused rather than resumed twice.
- **Switch a conversation's permission mode** among the agent's modes.
- **Teleport or Summon** from the chat header's split button. Use its arrow to
  choose an action, ordered Teleport, Summon, Fork. The button selects the first
  available action unless you choose another available one. Your choice stays
  selected when switching threads; an unavailable choice temporarily falls back
  to the first available action. Disabled choices show their reason. Teleport
  asks for a destination. Summon also offers agent rows with model chips, an
  editable brief, and an effort slider when the selected route supports it.
  Leave effort at Agent default to use the agent’s setting.
  Run output streams into the message panel. After the server confirms arrival,
  the console opens the destination thread if you are still viewing the source.
- **Account**: change password, issue and revoke API keys, unlink channel profiles,
  start a Slack or Discord profile link.
- **MCP**: install connectors from the configured offerings or by URL, authorise
  them, enable, disable and uninstall.

Every Trunkline conversation is a private thread; there is no DM surface or
nested sub-thread. It never needs profile linking: you are your signed-in account.
Incoming Teleport and Summon crossings are unavailable until the gateway supports
creating a top-level thread directly. The existing crossing path requires a DM
and a sub-thread inside it.

### Thread operation API

The backend exposes `GET /api/trunkline/threads/{id}/operations` with eligible
Teleport destinations and Summon agent/model routes, plus reasons when unavailable.
The header uses this response to populate its choices and refreshes it when you
open the action controls.

`POST /api/trunkline/threads/{id}/teleport` accepts a destination and an opening
hint. `POST /api/trunkline/threads/{id}/summon` also requires an agent, model and
brief (up to 8,000 characters). Both require access to the source thread and
refuse active gateway turns or pending approvals/questions. They stream native
run events, ending with `gateway` and the destination address; execution
failures appear as `run_error` events.

Native teleport and harnesses without independent session forking remain
unavailable. External thread rows do not preserve their parent surface's privacy,
so the API refuses to export their full history across channels. Summon transfers
only the supplied brief.

Destination visibility is still incomplete for newly created crossing threads.
If the move succeeds but the destination is absent from your accessible thread
list, the console reports that limitation in the message panel instead of
claiming navigation succeeded.

## The console surface

One screen: a threads sidebar with a channel rail, the chat ledger in the middle, a
timeline of the run's events on the right, a control rail for agents, MCP, profile,
keys and settings, and a review panel for a workspace's changes. Live data streams
over server-sent events; a browser that disconnects mid-run only stops watching,
the run finishes and records regardless.
