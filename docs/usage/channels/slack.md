# Slack

Slack connects over **Socket Mode**: the bot dials out, and events, block actions
and the assistant pane all arrive on that socket. No request URL, no public ingress.

## Create the app

In [api.slack.com/apps](https://api.slack.com/apps), create an app and:

1. **Socket Mode**: enable it and create an app-level token with
   `connections:write`. That is `app_token`, an `xapp-` value.
2. **Agents & AI Apps**: enable the assistant feature. This gives the bot its
   private assistant pane and the status line it sets while working.
3. **OAuth & Permissions**, bot token scopes, from what the bot calls:
   `chat:write`, `users:read`, `im:write`, `im:history`, `channels:history`,
   `groups:history`, `mpim:history`, `files:write`, `files:read`, `assistant:write`.
   Install the app to the workspace; the bot token is `bot_token`, an `xoxb-` value.
4. **Event Subscriptions**: subscribe the bot to `message.channels`,
   `message.groups`, `message.im`, `message.mpim`, `assistant_thread_started` and
   `assistant_thread_context_changed`.
5. **Interactivity & Shortcuts**: on. With Socket Mode no URL is needed.

The app id from **Basic Information** is `app_id`. If Slack refuses a call, its
error names the missing scope.

## Configure

```yaml
tentacles:
  slack:
    type: slack
    app_id: A0123456789
    agents: [inkling, claude]
    mention_only: true
```

```dotenv
OCTOMATE__TENTACLES__SLACK__BOT_TOKEN=xoxb-...
OCTOMATE__TENTACLES__SLACK__APP_TOKEN=xapp-...
```

Name agents you have already enabled and set `enabled: true` if this channel was
generated disabled. Check the configuration, restart Octomate, invite the bot to a
channel and `@`-mention it, or open its assistant pane. Verify a reply.

## Where things land

| Slack surface | In Octomate | The reply |
|---|---|---|
| A channel or group message | A group chat room | A new thread under a message the bot posts |
| A reply in a thread | That thread | In the same thread |
| The bot's DM | A direct message | A thread in the DM, because Slack only streams into threads |
| The assistant pane | A private thread | In the pane |

Slack's streaming API takes a thread and nothing else, so the bot never streams
into a channel root. When it opens a sub-thread it posts the hint as a message and
that message becomes the thread. A private hand-off opens a thread inside your DM
the same way.

Text over Slack's limit is uploaded as a Markdown file with a one-line note.

## Rendering

A run alternates between a **plan** stream and answer text. Thinking blocks and tool
calls become tasks in the plan, expanded while they run and folded when done, with
arguments and results inside. Todos are tasks in the same plan; Slack has no
"blocked" state, so a blocked todo shows as pending. Each answer text part is its
own streamed message, and the assistant status line reads "Thinking", "Writing the
response" or "Waiting for your input" as the turn moves.

Approvals arrive as one paged message per batch with Approve and Deny buttons.
Questions are a small wizard: radio buttons for choices, a free-text field, Back,
Next and Submit. The buttons carry the batch's state, so they keep working across
a restart.

## MCP tools acting as the person

Slack's channel tentacle is also an MCP connector. With `mcp: true`, agents on any
channel can search Slack, read channels, threads and profiles, and draft canvases
**as the person who asked**, once that person has connected. Nothing posts as them:
to say something in Slack, the agent uses `gateway_send`.

That needs the app as an OAuth client:

```yaml
tentacles:
  slack:
    type: slack
    mcp: true
    oauth:
      client_id: "123.456"
      callback_base_uri: https://octomate.example.com
```

```dotenv
OCTOMATE__TENTACLES__SLACK__OAUTH__CLIENT_SECRET=...
OCTOMATE__OAUTH__ENCRYPTION_KEY=...
```

In the app's settings, switch on the **Slack Model Context Protocol (MCP) Server**
feature under Agents, register `<callback_base_uri>/oauth/slack/callback` as a
redirect URL, and list the user token scopes Octomate asks for under **User Token
Scopes**. The default set is the eighteen the forwarded tools need, searches and
reads plus canvases, deliberately without `chat:write`. Widening the list later
means every connected user reconnects.

Each user connects once, from a chat, by asking the agent to connect Slack, or from
Trunkline. See [MCP proxy](../mcp/proxy.md).

## Profile linking

With the `oauth` block configured, a user can also link their Slack identity to
their account from Trunkline's Profile page, which verifies the workspace matches
this channel's. Without it, linking works from the chat as on every channel.

## Limits

- A stream is rotated after about four and a half minutes, Slack's limit, so a very
  long answer becomes several messages.
- Slash commands are not registered.
- A reply to one of the bot's messages does not count as addressing it; mention it.
