# Discord

Discord connects over one Gateway WebSocket for messages and button interactions,
and uses the REST API to send, edit, attach, open DMs and create threads. It dials
out: no interactions endpoint URL, no webhook.

## Create the app

In the [Developer Portal](https://discord.com/developers/applications):

1. Create an application and add its bot user. Leave **Public Bot** off; only you
   can then install it.
2. Under **Privileged Gateway Intents**, enable **Message Content**. Presence and
   Server Members stay off.
3. Under **OAuth2 → URL Generator**, select the `bot` scope with View Channels,
   Send Messages, Create Public Threads, Create Private Threads, Send Messages in
   Threads, Attach Files and Read Message History. Open the generated URL to add the bot to your server.
4. **Bot → Token** is `bot_token`.

## Configure

```yaml
tentacles:
  discord:
    type: discord
    agents: [inkling]
    mention_only: true
    stream:
      enabled: true          # off by default
      flush_interval: 0.2
```

```dotenv
OCTOMATE__TENTACLES__DISCORD__BOT_TOKEN=...
```

Name agents you have already enabled, set `enabled: true` if this channel was
generated disabled, then check the configuration and restart Octomate. Mention
the bot in a server text channel and verify that it replies in a new thread.

## Where things land

| Discord surface | In Octomate | The reply |
|---|---|---|
| A text channel message | A group chat room | A new public thread under a message the bot posts |
| A message in a thread | That thread | In the thread |
| A message in a private thread | That thread, private to its members | In the thread |
| A DM | A direct message | In the DM |

Discord is the one platform where replying to a bot message counts as addressing
it, so `mention_only` accepts a mention **or** a reply. Once an agent owns a thread,
messages there need neither. A chat room's sub-threads are public, opened from a
message the bot posts and named from the hint.

A conversation moved or handed to Discord lands in a text or forum channel. From a
conversation only you can read, a text channel opens a private thread holding you
and the bot, so the history stays private; server moderators who can manage
threads can still see it. From a shared conversation the thread is public. A forum
channel always takes a public post, opened with the hint. A message from a private
thread reads as private, so it can teleport like a DM.

From a server text channel, Summon lists eligible text and forum channels in that
same server. Teleport keeps shared history under the current text-channel address.
Your account must be able to view the channel and send messages in threads. The bot
must be able to view and send there, send in threads, and create public or private
threads for the thread it opens. Membership and permissions are checked again when
a named destination opens. Suggestions never scan other servers. Without a server context, including
entry from Trunkline, Discord suggests no parent addresses. DMs and existing threads
also offer no new sub-thread locations.

Other servers are reached by browsing, one level at a time: first the servers you
share with the bot, then the channels you can see in one. A channel a thread
cannot start in is listed with the reason, such as a private channel the bot was
not added to, and cannot be picked. Listing servers checks your membership in
each server the bot is in, and happens only when the Discord level is opened. It
is served by the
[trunkline's HTTP API](../../api/tentacles/trunkline.md).

Teleport can target a known text or forum channel directly with a connected channel ID
and the text-channel ID in the address's `chat_id`, including from a private conversation on another platform.
The channel validates that parent without enumerating servers and rechecks access before
posting. The requesting user needs a linked Discord profile. This accepts text
and forum channels only, not a guild ID, DM or existing thread. See
[Teleport](../gateway.md#teleport) for the address contract. An empty `chat_id` is
refused: Discord has no default isolated thread destination.

## Rendering

Discord has no cards. Streaming means editing one message as text arrives, rolling
onto a new message at the 2000-character limit. With streaming off, the default,
the whole answer is sent once at the end. Thinking, tool calls and todos are not
drawn. Images and files are real attachments. Mentions are allowed only for users
the agent named.

Approvals are one message per action with Approve and Deny buttons. Questions are a
component view: a button per choice, or a menu when the question takes several
picks, an "Other" button that opens a modal for free
text, and Previous, Next and Submit. Button identities carry only ids, and the
action is reloaded from the database when pressed, so they survive a restart.
Answers typed but not yet submitted do not.

With streaming enabled, an approval or question flushes buffered answer text
without closing the message. The agent can continue updating that message while
one or more prompts remain unanswered.

## Profile linking

Optionally, let users link their Discord identity from Trunkline's Profile page:

```yaml
tentacles:
  discord:
    type: discord
    oauth:
      client_id: "1234567890"
```

```dotenv
OCTOMATE__TENTACLES__DISCORD__OAUTH__CLIENT_SECRET=...   # the OAuth2 secret, not the bot token
OCTOMATE__OAUTH__ENCRYPTION_KEY=...
```

Set `oauth.callback_base_uri` and register `<callback_base_uri>/oauth/discord/callback`
under **OAuth2 → Redirects**. The flow asks for `identify` only. From the chat,
linking works without any of this.

## Limits

- Voice, reactions, slash commands and embeds are out of scope.
- Only default messages and replies from server text channels, threads and DMs
  are read; system messages, webhooks and other bots are ignored.
- Inbound image attachments are downloaded; other attachment types are not.
