# Threads and chat rooms

Everything a person says to Octomate, on any surface, lands in one model. Getting
it right is what makes a Slack thread, a Discord thread, a Lark topic, a console
thread and a terminal session the same kind of thing afterwards.

## Two kinds of surface

A **chat room** is a place with no end: a direct message with the bot, a group
channel. People keep talking there for as long as the room exists. A **thread** is a
piece of work: a Slack thread, a Discord thread, a Lark topic, a console thread, a
native session. It starts, it finishes.

The difference decides where an agent's context lives. A thread owns its
conversations: every turn continues the same agent context, and that context can be
[bound to a project](projects.md). A chat room owns none. A message in a room wakes
the agent in a fresh **sub-thread** opened inside the room, one level deep, with an
empty context plus a recap of the room's recent messages. That is what stops a
year-old DM from being a year-old context.

The recap is per channel:

```yaml
recap:
  messages: 16      # recent room messages shown to a kick; 0 shows none
  characters: 1000  # where each one is cut; 0 leaves them whole
```

Inside Octomate a room and a thread are both `Thread` rows. A room is the row with
no parent; a sub-thread is a row that shares the room's address and points at it.
Every row is identified by its **address**: channel id, chat type, chat id, and the
platform's own thread id when there is one. A native session's address is its
runtime's pseudo-channel and its session id.

## What each platform maps to

| Platform | Chat room | Thread | Where the bot replies |
|---|---|---|---|
| Slack | A channel, or the bot's DM | A message thread, or the assistant pane | Always in a thread. From a room, under a message it posts first. |
| Lark | A group, or a one-to-one chat | A reply thread, keyed on its root message | In the thread; in the chat otherwise |
| Discord | A text channel, or a DM | A thread under a text channel | In the thread; in the channel or DM otherwise |
| Trunkline | None | Every console thread | In the thread |
| QQ | A group, or a private chat | None | In the chat, quoting the message |
| Native runtimes | None | Every session | Not applicable; Octomate only records |

A thread on every platform continues without triage: the next message in it is the
next turn of the same conversation, no mention needed.

## Who owns a thread

A thread has an **active agent** once a handoff pinned one: a summon, a scheme, or
a first turn in a flat thread. From then on every message there continues with that
agent and model, and the mention gate treats the thread as addressed. A room's
main surface is never pinned, so that a group channel does not route every
message from every user to one agent because of one exchange. The handoff record
is append-only, so "who took this over, from whom, and why" is a row, not a scroll
through the chat.

## The ledger

What people, bots and agents visibly said is the **chat ledger**: one row per
message, inbound or outbound, dated when it happened. It is what history search
reads, what a brief cites by handle, and what Trunkline shows. An agent's own model
context, its tool calls and thinking, is a separate **conversation** per agent per
thread, whose runs hold the transcripts. The two are joined so a reply can be traced
to the prompt that caused it, but a model never reads another agent's transcript,
only the ledger.

A message's **handle** is `#msg:<platform id>`, the id the platform gave it. It is
what a search hit shows, what a brief cites, what `send` quotes to reply to a
specific message, and what the history tools page around.

A thread's title is the first human line said in it, until the runtime names the
session itself. Threads a person can see are the ones where one of their linked
profiles has spoken: a silent member of a group has no access to it until they
speak.

## Native sessions

A session you run yourself is a thread on the runtime's own pseudo-channel,
`claude-native`, `codex-native` or `deepseek-native`, owned by the user whose token
the client presents. The prompt and answer of each turn are ledger rows; the turn's
full transcript is the run. A subagent is a child conversation under the same
thread. A session started under a declared project root on the server's own
machine is filed under that project.

## Per-channel adjustments

- **Slack** cannot stream into a channel root, so a room reply always becomes a new
  thread, and a private hand-off opens a thread inside your DM. A reply to one of
  the bot's messages does not count as addressing it.
- **Lark** keys a thread on its root message rather than the topic id, so a
  continuation always finds the same conversation. Same rule about replies.
- **Discord** counts a reply to the bot as addressing it, creates sub-threads only
  from a text channel, and reads every thread as shared, private ones included.
- **Trunkline** has no rooms and no DMs, so everything is a thread and nothing needs
  a mention.
- **QQ** has no threads at all; every reply quotes the message it answers, and a
  hand-off into a private chat lands in the chat itself.
