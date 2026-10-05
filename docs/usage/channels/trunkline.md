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
- **Answer approvals and questions**, for any channel's thread. A batch's
  questions are one card with a page per question, sent together. A batch
  already resolved is refused rather than resumed twice.
- **Switch a conversation's permission mode** among the agent's modes.
- **Teleport or Summon** from the chat header's split button. Use its arrow to
  choose an action, ordered Teleport, Summon. The button selects the first
  available action unless you choose another available one. Your choice stays
  selected when switching threads; an unavailable choice temporarily falls back
  to the first available action. Disabled choices show their reason. Teleport
  and Summon expand the composer instead of opening a dialog: the button, ×
  or Esc returns to chat with your draft kept, and ⌘↵ submits.
  Typing `/` in an existing thread's composer opens a command finder for the
  same two actions: `/summon <agent>` and `/teleport [destination]`.
  ↑↓ moves, ⇥ completes, ↵ runs and Esc dismisses it. `/summon` lists the
  agents Summon offers, with the model and effort to hand over, and opens the
  Summon composer on the one you pick. `/teleport` lists the connected
  channels and opens Teleport on the one you pick, or on the destination
  browser when you pick none. A command that is unavailable is dimmed and shows
  its reason. A line that names no command is sent as an ordinary message.
  Teleport needs a destination; your draft, if any, is sent as your next
  message once it lands in a Trunkline thread. Browse destinations one level at a time: the connected
  channels, then what each holds, loaded as you open it. The filter narrows the
  rows already loaded. A channel that does not run the conversation's agent is
  dimmed and says so. When a private conversation is about to land somewhere
  shared, the composer says who will be able to read the thread it continues in.
  A conversation about a project lands in a thread about the same project, in a
  workspace of its own that holds the work as it stood; a teleport never
  switches projects.
  Summon hands this conversation to another agent where it is, with your draft
  as the brief; to continue elsewhere as well, teleport first. A native session
  or a group's main channel cannot be summoned. Its chip picks the agent and
  model from the routes this channel runs, and sets effort on a scale that dims
  the levels the model does not take. The scale
  starts at the route's default effort where the agent reports one. Where it does
  not, the scale starts at Auto, which leaves the level to the agent.
  Run output streams into the message panel. After the server confirms arrival,
  the console opens the destination thread if you are still viewing the source.
  It does the same when the agent moves the conversation itself in the middle of
  a turn. When a run starts there, the console opens the thread then and streams
  the run into it.
- **Account**: change password, issue and revoke API keys, unlink channel profiles,
  start a Slack or Discord profile link.
- **MCP**: install connectors from the configured offerings or by URL, authorise
  them, enable, disable and uninstall.

Every Trunkline conversation is a private thread; there is no DM surface or
nested sub-thread. It never needs profile linking: you are your signed-in account.
Teleport can create a new Trunkline thread directly. Open Discord in
the destination browser to list the servers you share with the bot, then the
channels you can see in one. Selecting a text channel creates a private thread
when this conversation is private and a public one otherwise; a forum channel
takes a public post; each row says which. A channel a thread cannot start in is
dimmed and shows why.
When viewing a Discord conversation, its server's eligible channels are also
suggested at the top of that level. Existing Discord DMs are omitted because they
cannot hold an isolated new thread. Slack and Lark list your DM and the channels
or groups you and the bot are both in. NapCat is listed but
disabled: QQ has no threads, so nothing can land there.

### When Teleport and Summon are available

Teleport is unavailable when the conversation's agent is not connected or cannot
fork its session, when the conversation is shared and its chat can start no
sub-thread, or when no connected channel runs an agent that can continue it. A
Slack assistant pane, a thread in a Slack DM, a Lark one-to-one topic and a Discord
private thread are private and can teleport; a public Discord thread and a thread
in a Slack channel or Lark group are shared and cannot. A shared thread's full
history is never carried to another channel.

Summon is unavailable for a native session, a group's main channel, or a channel
that runs no other agent. It hands over only the brief.

A native Codex or Claude Code session teleports with its latest fully uploaded
turn, keeping its model and permissions. Native DeepSeek sessions, and driven
agents that cannot fork their session, cannot teleport yet. The thread a move
lands in shows up in your thread list and is about the same project as the one it
left.

The console's HTTP API, its request bodies and the events it streams are in the
[API reference](../../api/tentacles/trunkline.md).

## The console surface

One screen: a threads sidebar with a channel rail, the chat ledger in the middle, a
timeline of the run's events on the right, a control rail for agents, MCP, profile,
keys and settings, and a review panel for a workspace's changes. Live data streams
over server-sent events; a browser that disconnects mid-run only stops watching,
the run finishes and records regardless.
