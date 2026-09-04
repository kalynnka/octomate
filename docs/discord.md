# Discord channel

The Discord tentacle uses one long-lived Gateway WebSocket for inbound messages and
component interactions, and Discord's REST API for sends, edits, attachments, DMs,
and public-thread creation. It dials out: there is no Discord-facing HTTP server,
Interactions Endpoint URL, webhook, or Discord MCP server to configure.

## Create the private app

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, and add its bot user.
2. Under **Bot → Authorization Flow**, leave **Public Bot** off and **Requires OAuth2
   Code Grant** off. Only the application owner can install a private bot.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**. Leave
   Presence and Server Members off; Octomate does not request them.
4. Under **Installation**, keep **Install Link** set to **None**. Discord does not
   allow a private application to carry a default authorization link.
5. Under **OAuth2 → URL Generator**, select only the `bot` scope and these bot
   permissions:

   - View Channels
   - Send Messages
   - Create Public Threads
   - Send Messages in Threads
   - Attach Files
   - Read Message History

   Follow the generated URL while signed in as the application owner and add the bot
   to the intended server. Administrator and `applications.commands` are not needed.

Discord documents the
[bot authorization flow](https://docs.discord.com/developers/topics/oauth2#bot-authorization-flow),
[Gateway](https://docs.discord.com/developers/events/gateway), and
[Message Content intent](https://docs.discord.com/developers/events/gateway#message-content-intent)
separately. Button, select, and modal interactions arrive on the same Gateway
connection, so
[an Interactions Endpoint URL is not required](https://docs.discord.com/developers/interactions/overview#configuring-an-interactions-endpoint-url).

## Configure Octomate

Declare the channel's structure in `.octomate/config/channels.yaml`:

```yaml
channels:
  discord:
    type: discord
    mention_only: true
    agents:
      - agent: inkling
        model: anthropic:claude-sonnet-5
    stream:
      enabled: true
      flush_interval: 0.5
```

Keep the bot token out of YAML and Git. Put it in the checkout's `.env` instead:

```dotenv
OCTOMATE__CHANNELS__DISCORD__BOT_TOKEN=<bot token>
```

Restart Octomate after changing either file:

```bash
uv run octomate serve --tmux
```

With `mention_only: true`, a new server-channel conversation must mention the bot or
reply to one of its messages. DMs do not require a mention. Once an agent owns a public
thread, later messages in that thread continue it without another mention.

## Supported surfaces and limits

Discord renders streamed and long text through message edits and 2,000-character
chunks. It uploads outbound image and file segments, creates public threads, shows
approval buttons, choice selects, free-text modals, and sends OAuth links privately by
DM. Buttons and selects are persistent: their callback identity is carried by Discord,
and Octomate reloads the action from its database after a restart.

Inbound default messages and replies are supported in server text channels, public
threads, and DMs. Inbound image attachments are downloaded into the conversation;
other inbound file types, Discord system messages, private-thread creation, voice,
reactions, slash commands, and native Discord embeds are deliberately outside the
current surface.

## Live verification

Enable Developer Mode in Discord, then copy the text-channel id and your user id into
the gitignored `.octomate/config/trigger.yaml`:

```yaml
trigger:
  discord:
    chat_type: group
    chat_id: "<text channel id>"
    user_id: "<operator user id>"
```

Run the focused replay explicitly:

```bash
uv run pytest tests/trigger/test_discord.py -q
```

Every surface is a separate test, so a single case can be selected by node id:

```bash
uv run pytest \
  tests/trigger/test_discord.py::test_discord_renders_native_attachments -q
```

The tests write to the real server. When several are selected, they share one Gateway
client and one fresh public thread; a single selected test gets its own thread. Together
they send final and long streamed text, native image and file attachments, mid-run
notices, action controls, OAuth in the operator's DM, and parent/subagent output. The
reconnect case forces one Gateway disconnect and waits for reconnection. Every case
skips without both the Discord channel and target; the normal full suite never opts in.

After the replay, exercise inbound routing with the real Octomate service:

1. Mention the bot once in a server text channel.
2. Reply to a bot message.
3. DM the bot.
4. Continue the agent-owned public thread without mentioning the bot.

Each message should dispatch once. Inspect the thread and DM rendering in Discord, and
confirm the service log reports the Gateway reconnect without a duplicate dispatch.
