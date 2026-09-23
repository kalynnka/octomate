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
   Send Messages, Create Public Threads, Send Messages in Threads, Attach Files and
   Read Message History. Open the generated URL to add the bot to your server.
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

## Where things land

| Discord surface | In Octomate | The reply |
|---|---|---|
| A text channel message | A group chat room | A new public thread under a message the bot posts |
| A message in a thread | That thread | In the thread |
| A DM | A direct message | In the DM |

Discord is the one platform where replying to a bot message counts as addressing
it, so `mention_only` accepts a mention **or** a reply. Once an agent owns a thread,
messages there need neither. Sub-threads are created from a text channel only,
public, named from the hint.

## Rendering

Discord has no cards. Streaming means editing one message as text arrives, rolling
onto a new message at the 2000-character limit. With streaming off, the default,
the whole answer is sent once at the end. Thinking, tool calls and todos are not
drawn. Images and files are real attachments. Mentions are allowed only for users
the agent named.

Approvals are one message per action with Approve and Deny buttons. Questions are a
component view: a button per choice, an "Other" button that opens a modal for free
text, and Previous, Next and Submit. Button identities carry only ids, and the
action is reloaded from the database when pressed, so they survive a restart.
Answers typed but not yet submitted do not.

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

- Private threads, voice, reactions, slash commands and embeds are out of scope.
- Only default messages and replies from server text channels, public threads and
  DMs are read; system messages, webhooks and other bots are ignored.
- Inbound image attachments are downloaded; other attachment types are not.
