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
  project and reasoning effort on the first message. The effort applies to the
  first run and stays selected for following turns; Auto leaves it to the agent.
  In the agent/model picker, drag the effort slider or focus it and use ←→.
  A thread keeps its agent and model; a different
  pick on an owned thread is refused, because a mid-thread model switch busts the
  provider's cache. Hand off explicitly instead.
- **Read any thread**: the chat ledger, each agent conversation and its runs, the
  handoffs, and the pending actions.
- **Resize the input** by dragging the composer's top edge up or down. The chosen
  height stays while you type or use commands. Double-click the edge to return to
  automatic sizing; when the handle has keyboard focus, ↑ and ↓ adjust it.
- **Answer approvals and questions**, for driven conversations on any channel.
  A batch's questions are one card with a page per question, sent together. A batch
  already resolved is refused rather than resumed twice.
- **Switch a conversation's permission mode** among the agent's modes.
- **Set a conversation's reasoning effort** with `/effort`: a scale of the levels
  the thread's model takes, moved with ←→ or by dragging and applied with ↵. The
  level holds for the conversation's following turns, including command-triggered
  runs, until you change it, and a teleport keeps it; Auto leaves it to the agent.
  If the selected model no longer supports the saved level, change it or choose
  Auto before running again. A turn already running keeps the
  level it started at. The composer's route chip shows the level once one is set.
- **Teleport or Summon** from the chat header's split button. Use its arrow to
  choose an action, ordered Teleport, Summon. The button selects the first
  available action unless you choose another available one. Your choice stays
  selected when switching threads; an unavailable choice temporarily falls back
  to the first available action. Disabled choices show their reason. Teleport
  and Summon expand the composer instead of opening a dialog: the button, ×
  or Esc returns to chat with your draft kept, and ⌘↵ submits.
  Typing `/` in a new or existing thread's composer opens a command finder. The
  prompt arrow points up while the finder, Teleport or Summon is expanded. The
  gateway offers `/summon <agent> [--model <model>] [--effort <effort>]`, `/teleport [destination]`,
  `/effort [level]` and `/new`, alongside the commands the thread's agent offers in
  its own runtime, looked up when you type `/` for the selected agent and settings.
  The finder groups commands by source: **Gateway**, then the agent's native
  commands. Within each group, rows are marked and ordered **Available**,
  **Unavailable here** (a prerequisite is missing), then **Not supported yet**.
  Disabled commands explain what prevents their use.
  Before the first message, commands that need a conversation stay unavailable
  and explain why. Commands the agent makes available without a conversation
  show their direct feedback in the message panel without opening a thread; this
  feedback is not saved in thread history. `/effort` sets the first message's
  effort locally. Summon and Teleport require an existing conversation.
  Synced native sessions are read-only: messages, native commands, permission
  and effort changes, and approval answers are unavailable. Use an available
  gateway operation such as Teleport to continue elsewhere. Type to narrow the
  suggestions; moving the pointer over them does not change the selection. Arrow keys move the text
  cursor. Tab completes, Enter selects or runs, and Esc dismisses the finder.
  Accepting a partial name completes it: `/te` becomes `/teleport` when its menu opens.
  Deleting the leading `/` returns to ordinary input. `/summon` lists the
  agents Summon offers, with the model and effort to hand over, and opens the
  Summon composer on the one you pick. `/teleport` lists the connected
  channels and opens Teleport on the one you pick, or on the destination
  browser when you pick none. `/new` opens the composer for a new thread.
  Submitting `/teleport trunkline` moves directly to a new Trunkline thread.
  A destination that still needs a server or channel choice opens its menu instead.
  Codex's `/model` and `/project` open the same model and project pickers as the
  buttons. From an existing conversation, they open a new composer and leave
  that conversation unchanged. `/task` opens a projectless composer; `/worktree`
  opens the project picker for a new conversation with its own workspace.
  These controls take no arguments and create nothing until the first prompt.
  Clicking a Teleport or Summon option opens the same form as the header button,
  keeping the chosen agent, model, effort or destination.
  If the command still needs an agent or destination, its menu opens and the
  command stays in the input until you choose one. Then enter the brief or
  optional prompt. A destination that cannot be used shows the same reason in
  the finder and its menu.
  The form keeps these choices when closed and reopened on the same thread.
  The agent/model picker expands above the input. Its model list scrolls,
  while the effort slider stays below it. Effort uses a single slider, with
  room for each advertised level.
  The command finder shows up to six model candidates. Type `--model` followed
  by part of a model name to fuzzy-filter them, or choose **Show all models**
  to open the full picker. The selected model stays visible when it matches.
  For example, `/summon claude --model sonnet --effort high` selects a matching
  model at high effort. Both options are optional and can appear in either order.
  Omitting model keeps the selected model or uses the first offered model;
  omitting effort uses the selected model's default. Tab completes option names
  and values, and moving the slider updates `--effort` in the command text.
  `/effort` without a level restores the
  current conversation's default effort.
  In the agent/model and destination menus, Up/Down moves between choices,
  Home/End jumps to the first or last, Enter selects, and Esc closes the menu.
  In destination lists, Right opens a place and Left returns to its parent.
  An agent's own command takes what you type after it, exactly as typed, and what
  it answers streams into the message panel like a turn. Its input hint is
  shown as advertised by the harness or skill; when it supplies no argument
  structure, the harness decides which inputs are required. Tab completes
  names and advertised choices without inserting placeholder text. A native
  command that shares a gateway name appears as `/native:<name>`.
  A command that is
  unavailable is dimmed and shows its reason, and when the agent's commands
  cannot be looked up the finder says why. A line that names no command is sent
  as an ordinary message.
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
  model from the routes this channel runs, and sets effort from the native
  levels the model advertises. The scale
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
left. A new destination starts with the source's name: the one its agent gave the
session, or else the source thread's title. An agent that names its sessions renames
the destination as its work goes on, and a source with no name leaves the
destination to take one from the first thing you say in it. The destination
carries the agent's history run by run; work its subagents did stays with the
source. A move from a thread also shows that thread's chat, with its senders and
attachments. A move from a DM or group chat brings only the agent's history: the
chat room's messages stay in the chat room, where history search still finds
them. From there the two threads grow apart: what happens in one never appears in
the other.
Inheriting someone's messages does not give them access to the destination or
its later messages through history search.
The continuation notice appears in the destination; the source Trunkline thread
gets no extra chat message.

The console's HTTP API, its request bodies and the events it streams are in the
[API reference](../../api/tentacles/trunkline.md).

## The console surface

One screen: a threads sidebar with a channel rail, the chat ledger in the middle, a
timeline on the right that groups each conversation's messages under the turns
that drew them, each group folding on its own, a control rail for agents, MCP, profile,
keys and settings, and a review panel for a workspace's changes. On desktop, hover
over the channel rail to replace its initials with channel names and thread counts;
moving away restores the initials. Teleport continuation notices use information blue.
Ordinary chat runs stream over server-sent events; a browser that disconnects mid-run only stops
watching, and the run finishes and records regardless.

Explicit commands submitted through `POST /api/commands/execute` use Reflex for
user tools, approvals and reply history. Direct feedback and agent activity use
the normal channel stream, followed by a command outcome for completion tracking.
Clients display the channel events without displaying the outcome again. A command
request's disconnect cancels that execution; cleanup and outcome recording may be
interrupted. Retry with the same delivery ID to read the recorded outcome. If the
receipt has no outcome, the retry is refused to avoid running the command twice.
