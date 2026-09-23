# Threads and chat rooms

Use a **thread** for one piece of work. Keep replying there to continue with its
context. A **chat room** is the broader place you talk: a group channel or a direct
message with the bot. It may contain many unrelated tasks.

## Start in a room, continue in a thread { #two-kinds-of-surface }

Mention the bot in a shared room and describe your task. When it opens a thread,
follow the reply there and keep subsequent messages together. Once an agent is
handling that thread, you can reply without mentioning it each time.

> Help me plan the launch. Start with the decisions we need to make this week.

Then, in the reply thread:

> Turn those decisions into a checklist and flag what still needs my input.

A new request in the room can start with only recent room messages for context.
For a follow-up that depends on earlier decisions, return to the task's thread.
Start a separate thread when the subject changes.

## Find the reply { #what-each-platform-maps-to }

| Where you start | Where to continue |
|---|---|
| Slack channel or bot DM | In the bot's reply thread |
| Slack assistant pane | In that pane's conversation |
| Lark / Feishu group | In the reply thread the bot opens |
| Lark / Feishu one-to-one chat | In that chat |
| Discord server text channel | In the public reply thread |
| Discord DM | In that DM |
| NapCat group or DM | In the same chat; there are no subthreads |
| Trunkline | In the conversation you opened |
| Your agent's own app, editor or terminal | In that agent's session; Trunkline lets you read the collected history |

## Keep the same agent, or hand over { #who-owns-a-thread }

Follow-ups in an active thread go to the agent handling it. If another agent would
be better suited, ask it to [hand over the task](gateway.md#summon), including the
result you want and any decisions that must carry forward.

For work on files, [choose a project](projects.md) before the agent starts making
changes. Use a new thread for a different project.

## Read your conversations

Open a thread in Trunkline to review messages, the agent's activity and pending
requests. Your available history comes from conversations you participated in
through your linked profiles; joining a group alone does not add its whole
history to your account. [History](history.md) explains how to find an earlier
conversation.

## Channel differences { #per-channel-adjustments }

- **Slack and Lark:** mention the bot for a new task in a shared room. Replying to
  its message alone does not count as a mention outside an active agent thread.
- **Discord:** mentioning the bot or replying to its message can address it.
  New task threads are public, so use a DM for private requests.
- **Trunkline:** each conversation is already a thread. No mention is needed.
- **NapCat:** mention the bot for each request in a group when `mention_only` is
  enabled. DMs need no mention. Quoted replies stay in the same chat and do not
  open a task thread; new requests use the recent chat recap.

To continue through another channel, follow [Moving a conversation](gateway.md).
