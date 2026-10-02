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
  and Summon expand the composer instead of opening a dialog: the button, ×
  or Esc returns to chat with your draft kept, and ⌘↵ submits.
  Typing `/` in an existing thread's composer opens a command finder for the
  same three actions: `/summon <agent>`, `/teleport [destination]` and `/fork`.
  ↑↓ moves, ⇥ completes, ↵ runs and Esc dismisses it. `/summon` lists the
  agents Summon offers, with the model and effort to hand over, and opens the
  Summon composer on the one you pick. `/teleport` lists the connected
  channels and opens Teleport on the one you pick, or on the destination
  browser when you pick none. `/fork` forks at once. A command that is
  unavailable is dimmed and shows its reason. A line that names no command is
  sent as an ordinary message.
  Teleport needs a destination and takes an optional note, which opens the
  thread there. Browse destinations one level at a time: the connected
  channels, then what each holds, loaded as you open it. The filter narrows the
  rows already loaded.
  Summon uses your draft as the brief and hands this conversation over in
  place whenever that is offered; otherwise it asks for a destination too. Its
  chip picks the agent and model from the destination channel's routes, and
  sets effort on a scale that dims the levels the model does not take. The scale
  starts at the route's default effort where the agent reports one. Where it does
  not, the scale starts at Auto, which leaves the level to the agent.
  Run output streams into the message panel. After the server confirms arrival,
  the console opens the destination thread if you are still viewing the source.
- **Account**: change password, issue and revoke API keys, unlink channel profiles,
  start a Slack or Discord profile link.
- **MCP**: install connectors from the configured offerings or by URL, authorise
  them, enable, disable and uninstall.

Every Trunkline conversation is a private thread; there is no DM surface or
nested sub-thread. It never needs profile linking: you are your signed-in account.
Teleport and Summon can create a new Trunkline thread directly. Open Discord in
the destination browser to list the servers you share with the bot, then the
channels you can see in one; selecting one creates a public Discord thread. A
channel a thread cannot start in is dimmed and shows why.
When viewing a Discord conversation, its server's eligible channels are also
suggested at the top of that level. Existing Discord DMs are omitted because they
cannot hold an isolated new thread. Slack, Lark and NapCat show only their
suggested destinations and say that they cannot be browsed.

### Thread operation API

The backend exposes `GET /api/trunkline/threads/{id}/operations` with eligible
Teleport and Summon destination addresses, an optional `here` address for in-place
Summon, and agent/model `routes` keyed by connected channel ID, plus reasons when
unavailable.
The header uses this response to enable its choices and refreshes it when you
open the action controls; an operation is unavailable only when its `reason` is
set. An empty list of suggestions sets none by itself, since the destination
browser can still find a place. Teleport's reason is set when the source agent is
not connected or cannot fork its session, when the conversation is shared and its
chat can start no sub-thread, or when no connected channel runs an agent that can
continue the history. Trunkline treats every conversation from another channel as
shared unless it is a direct message, so a Discord thread cannot teleport.
Summon's reason is set only when no other agent is connected.

`GET /api/trunkline/threads/{id}/channels/{channel}/addresses` lists one level of
a connected channel's destinations, fetched when that level is opened. Each row
is a `ChannelAddress` whose `metadata` carries its `name`. A row with
`metadata.inside` is a place to open: pass that value as `?inside=` to list what
it holds. A row without it is an address a thread can land in, unless
`metadata.barred` says why it cannot. Discord lists servers, then the channels
you can see in one. A channel that cannot be browsed, has no connected agent, or
is not linked to your account answers 409 with the reason. A listed address is a
suggestion: it is validated again when Teleport or Summon submits it. The
destination browser calls it once per level opened.

`POST /api/trunkline/threads/{id}/teleport` accepts a `ChannelAddress` as
`destination`, a `new_thread` flag (true by default), and an opening hint.
The console sends `new_thread=false` only for Summon's in-place handover.
`POST /api/trunkline/threads/{id}/summon` also requires an agent, model and
brief (up to 8,000 characters). Both require access to the source thread and
refuse active gateway turns or pending approvals/questions. They stream native
run events, ending with `gateway` and the destination address; execution
failures appear as `run_error` events.

Native Codex teleport imports the latest fully uploaded completed turn through
the existing transcript fork, preserving its model and permissions. It uses
the first compatible Codex agent in the selected destination's route order. Other native harnesses and driven harnesses without
independent session forking remain unavailable. External thread rows do not
preserve their parent surface's privacy,
so the API refuses to export their full history across channels. Summon transfers
only the supplied brief.

The destination's existing opening message is recorded with the requesting
user's identity so the new thread appears in their list. Native Codex's fork
notice provides this attribution instead. No additional arrival hint is sent.

## The console surface

One screen: a threads sidebar with a channel rail, the chat ledger in the middle, a
timeline of the run's events on the right, a control rail for agents, MCP, profile,
keys and settings, and a review panel for a workspace's changes. Live data streams
over server-sent events; a browser that disconnects mid-run only stops watching,
the run finishes and records regardless.
